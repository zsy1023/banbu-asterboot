"""后台监控引擎：拉/推两种源的统一循环 + 事件产出的唯一入口。

对上只暴露：on_snapshot(快照) / on_events(事件, 快照) / on_error(异常, 阶段)。
不依赖 astrbot，也不调 LLM —— 事件之后怎么发、发什么，由上层决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Awaitable, Callable

from .adapters.base import DeviceAdapter
from .compat import Profile
from .events import EventState, detect, detect_offline, online_event
from .models import DeviceEvent, Snapshot, now
from .rules import RuleEngine

SnapshotCb = Callable[[Snapshot], Awaitable[None]]
EventsCb = Callable[[list[DeviceEvent], Snapshot], Awaitable[None]]
ErrorCb = Callable[[Exception, str], None]
ConfigGet = Callable[[str, Any], Any]


class MonitorEngine:
    def __init__(
        self,
        adapter: DeviceAdapter,
        profile: Profile,
        rules: RuleEngine,
        state: EventState,
        config: ConfigGet,
        on_snapshot: SnapshotCb | None = None,
        on_events: EventsCb | None = None,
        on_error: ErrorCb | None = None,
        logger: Any = None,
    ):
        self.adapter = adapter
        self.profile = profile
        self.rules = rules
        self.state = state
        self._config = config
        self._on_snapshot = on_snapshot
        self._on_events = on_events
        self._on_error = on_error
        self.log = logger

        self._task: asyncio.Task | None = None
        self._running = False
        self._failures = 0
        self._last_error = ""
        self._active_source = "idle"
        self.refresh_requested = asyncio.Event()

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="bambu-a1-monitor")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            self.adapter.stop_stream()  # type: ignore[attr-defined]

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        prev = self.state.last_snapshot
        return {
            "running": self._running,
            "source": self._active_source,
            "mode": self._config("monitor_mode", "auto"),
            "failures": self._failures,
            "last_error": self._last_error,
            "last_data_age_s": int(now() - prev.ts) if prev else None,
            "offline": self.state.offline,
            "adapter": self.adapter.key,
        }

    # ── 主循环 ──────────────────────────────────────────────
    async def _run(self) -> None:
        mode = str(self._config("monitor_mode", "auto") or "auto")
        try:
            if mode == "poll":
                await self._poll_forever()
                return
            if mode == "stream":
                await self._stream_forever()
                return
            # auto：长连接优先，连续失败降级轮询，稍后再试
            await self._auto_forever()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._report_error(exc, "loop")
        finally:
            self._running = False

    async def _auto_forever(self) -> None:
        while self._running:
            if self._adapter_has_push():
                try:
                    await self._stream_forever(allow_give_up=True)
                    continue
                except _GiveUp:
                    self._note("长连接不可用，降级为轮询")
            await self._poll_forever(
                duration=float(self._config("poll_interval", 30) or 30) * 10
            )

    def _adapter_has_push(self) -> bool:
        try:
            return self.adapter.push_source() is not None
        except NotImplementedError:
            return False
        except Exception:  # noqa: BLE001
            return False

    def _adapter_has_poll(self) -> bool:
        try:
            return self.adapter.poll_source() is not None
        except NotImplementedError:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _stream_forever(self, allow_give_up: bool = False) -> None:
        """长连接常驻；断了自动重连，auto 模式下第一次失败就交给轮询兜底。"""
        consecutive = 0
        while self._running:
            if not self._adapter_has_push():
                raise _GiveUp()
            try:
                await self._stream_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                consecutive += 1
                self._report_error(exc, "stream")
                if allow_give_up:
                    raise _GiveUp() from exc
                await asyncio.sleep(min(60, 5 * consecutive))
            else:
                consecutive = 0
                if not self._running:
                    return
                await asyncio.sleep(3)

    async def _stream_session(self) -> None:
        self._active_source = "stream"
        source = self.adapter.push_source()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)

        async def pump() -> None:
            async for raw in source.stream():  # type: ignore[union-attr]
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                await queue.put(raw)

        pump_task = asyncio.create_task(pump(), name="bambu-a1-mqtt-pump")
        watchdog = max(5.0, float(self._config("stale_after", 90) or 90) / 3)
        try:
            while self._running:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=watchdog)
                except asyncio.TimeoutError:
                    await self._check_stale()
                    if pump_task.done():
                        exc = pump_task.exception() if not pump_task.cancelled() else None
                        raise RuntimeError(f"长连接已断开: {exc}")
                    continue
                self._failures = 0
                await self._handle_raw(raw)
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task

    async def _poll_forever(self, duration: float | None = None) -> None:
        if not self._adapter_has_poll():
            self._report_error(RuntimeError("该设备没有可用的拉取源"), "poll")
            await asyncio.sleep(30)
            return
        self._active_source = "poll"
        source = self.adapter.poll_source()
        interval = max(5, int(self._config("poll_interval", 30) or 30))
        deadline = time.monotonic() + duration if duration else None

        while self._running:
            if deadline and time.monotonic() > deadline:
                return
            try:
                raw = await source.fetch()  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                raw = None
                self._report_error(exc, "poll")

            if raw:
                self._failures = 0
                await self._handle_raw(raw)
            else:
                self._failures += 1
                await self._check_stale()
                # 指数退避，上限 10 分钟，避免被云端限流
                backoff = min(600, interval * (2 ** min(self._failures, 5)))
                if self._failures >= 2:
                    await asyncio.sleep(backoff - interval)

            try:
                await asyncio.wait_for(self.refresh_requested.wait(), timeout=interval)
                self.refresh_requested.clear()
            except asyncio.TimeoutError:
                pass

    # ── 单次处理 ────────────────────────────────────────────
    async def _handle_raw(self, raw: dict[str, Any]) -> None:
        snap = self.adapter.to_snapshot(raw)
        if not snap.connected:
            return

        prev = self.state.last_snapshot
        first_poll = prev is None
        milestones = self._config("progress_milestones", []) or []

        if snap.task_id or snap.task_name:
            self.state.sync_task(snap)

        events = detect(prev, snap, self.profile, milestones=milestones, first_poll=first_poll)
        events.extend(self.rules.evaluate(prev, snap))
        events = self.state.filter_new(events)

        was_offline = self.state.offline
        self.state.offline = False
        recover = online_event(snap, was_offline)
        if recover and self.state.remember(recover.dedup_key):
            events.append(recover)

        self.state.save(snap)

        if self._on_snapshot:
            with contextlib.suppress(Exception):
                await self._on_snapshot(snap)
        if events and self._on_events:
            try:
                await self._on_events(events, snap)
            except Exception as exc:  # noqa: BLE001
                self._report_error(exc, "dispatch")

    async def _check_stale(self) -> None:
        stale_after = self._offline_threshold_s()
        prev = self.state.last_snapshot
        evt = detect_offline(prev, stale_after)
        if not evt:
            return
        if self.state.offline:
            return
        self.state.offline = True
        self.state.save()
        if self.state.remember(evt.dedup_key) and self._on_events and prev:
            with contextlib.suppress(Exception):
                await self._on_events([evt], prev)

    def _offline_threshold_s(self) -> int:
        """配置的 stale_after 与用户 offline 规则取较小值。"""
        base = int(self._config("stale_after", 90) or 90)
        try:
            minutes = self.rules.offline_threshold_minutes()
        except Exception:  # noqa: BLE001
            minutes = None
        if minutes:
            return max(10, min(base, int(minutes * 60)))
        return base

    async def refresh_now(self) -> bool:
        """强制刷新一次（命令用）。轮询模式下唤醒循环；推模式下靠连接自身心跳。"""
        self.refresh_requested.set()
        if self._adapter_has_poll():
            try:
                raw = await self.adapter.poll_source().fetch()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                self._report_error(exc, "refresh")
                return False
            if raw:
                await self._handle_raw(raw)
                return True
        return False

    # ── 杂项 ────────────────────────────────────────────────
    def _note(self, message: str) -> None:
        if self.log:
            with contextlib.suppress(Exception):
                self.log.info(message)

    def _report_error(self, exc: Exception, phase: str) -> None:
        self._last_error = f"{phase}: {type(exc).__name__}: {exc}"
        if self.log:
            with contextlib.suppress(Exception):
                self.log.warning("监控异常(%s): %s: %s", phase, type(exc).__name__, exc)
        if self._on_error:
            with contextlib.suppress(Exception):
                self._on_error(exc, phase)


class _GiveUp(Exception):
    """auto 模式下长连接不可用，交给轮询兜底。"""
