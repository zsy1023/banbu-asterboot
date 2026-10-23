"""合并窗口：窗口内的事件（+ 模型补写）合成一条消息。

规则（ARCHITECTURE.md 6.4）：
  - direct 事件立即进窗口，窗口到点统一发出
  - llm 事件并行发起补写；窗口内返回就合并，超窗就只发事实（告警永不丢）
  - 同设备、同订阅者才合并；去重在上游（EventState）已完成
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable

from .models import CH_BOTH, CH_LLM, SEV_CRITICAL, DeviceEvent

FlushCb = Callable[[list[DeviceEvent]], Awaitable[None]]
DraftCb = Callable[[DeviceEvent], Awaitable[str | None]]


class Coalescer:
    def __init__(
        self,
        sink: FlushCb,
        *,
        window_s: float = 3.0,
        critical_window_s: float = 0.8,
        draft: DraftCb | None = None,
        logger: Any = None,
    ):
        self._sink = sink
        self.window_s = max(0.0, float(window_s or 0))
        self.critical_window_s = max(0.0, float(critical_window_s or 0))
        self._draft = draft
        self._log = logger
        self._buffers: dict[str, list[DeviceEvent]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._drafts: dict[int, asyncio.Task] = {}

    # ── 提交 ────────────────────────────────────────────────
    async def submit(self, events: list[DeviceEvent]) -> None:
        if not events:
            return
        for event in events:
            key = event.device or "default"
            self._buffers.setdefault(key, []).append(event)
            if event.channel in (CH_LLM, CH_BOTH) and self._draft:
                task = asyncio.create_task(self._run_draft(event), name=f"draft-{event.type}")
                self._drafts[id(event)] = task
        for key in list(self._buffers):
            self._schedule(key)

    async def _run_draft(self, event: DeviceEvent) -> None:
        try:
            text = await self._draft(event)  # type: ignore[misc]
            if text:
                event.data["llm_text"] = text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._log:
                with contextlib.suppress(Exception):
                    self._log.warning("LLM 补写失败(%s): %s: %s", event.type, type(exc).__name__, exc)
        finally:
            self._drafts.pop(id(event), None)

    # ── 窗口 ────────────────────────────────────────────────
    def _delay(self, events: list[DeviceEvent]) -> float:
        if self.window_s <= 0:
            return 0.0
        if any(e.severity == SEV_CRITICAL for e in events):
            return self.critical_window_s
        return self.window_s

    def _schedule(self, key: str) -> None:
        events = self._buffers.get(key) or []
        if not events:
            return
        delay = self._delay(events)
        old = self._timers.pop(key, None)
        if old and not old.done():
            old.cancel()
        if delay <= 0:
            task = asyncio.create_task(self._flush(key), name=f"flush-{key}")
        else:
            task = asyncio.create_task(self._delayed_flush(key, delay), name=f"flush-{key}")
        self._timers[key] = task

    async def _delayed_flush(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush(key)

    async def _flush(self, key: str) -> None:
        events = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not events:
            return
        # 把已经完成的模型补写落到事件上；没完成的直接丢弃（已超窗）
        for event in events:
            task = self._drafts.pop(id(event), None)
            if task is None:
                continue
            if task.done() and not task.cancelled():
                with contextlib.suppress(Exception):
                    text = task.result()
                    if text:
                        event.data["llm_text"] = text
            else:
                task.cancel()
        events.sort(key=lambda e: (0 if e.severity == SEV_CRITICAL else 1, e.ts))
        try:
            await self._sink(events)
        except Exception as exc:  # noqa: BLE001
            if self._log:
                with contextlib.suppress(Exception):
                    self._log.error("推送失败: %s: %s", type(exc).__name__, exc)

    async def flush_all(self) -> None:
        for task in list(self._timers.values()):
            if not task.done():
                task.cancel()
        self._timers.clear()
        for key in list(self._buffers):
            await self._flush(key)

    async def cancel_pending(self) -> None:
        for task in list(self._timers.values()):
            if not task.done():
                task.cancel()
        self._timers.clear()
        for task in list(self._drafts.values()):
            if not task.done():
                task.cancel()
        self._drafts.clear()
        self._buffers.clear()
