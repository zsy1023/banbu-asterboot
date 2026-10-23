"""推送：订阅者管理 + 安静时段 + 主动消息投递。

推送目标 = "谁绑定就推给谁"：绑定/订阅时记录会话 umo，之后都发到这些会话。
不导入 astrbot 的顶层符号（MessageChain 在函数内导入），保证本模块可单测。
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from . import render
from .models import SEV_CRITICAL, DeviceEvent
from .store import read_json, write_json


def parse_quiet_range(spec: str) -> tuple[int, int] | None:
    """"23:00-07:30" → (1380, 450)，分钟数；非法返回 None。"""
    spec = str(spec or "").strip()
    if "-" not in spec:
        return None
    left, _, right = spec.partition("-")
    try:
        lh, lm = [int(x) for x in left.strip().split(":")]
        rh, rm = [int(x) for x in right.strip().split(":")]
    except (ValueError, TypeError):
        return None
    if not (0 <= lh <= 23 and 0 <= rh <= 23 and 0 <= lm <= 59 and 0 <= rm <= 59):
        return None
    return lh * 60 + lm, rh * 60 + rm


def in_quiet_hours(spec: str, minutes_now: int) -> bool:
    parsed = parse_quiet_range(spec)
    if not parsed:
        return False
    start, end = parsed
    if start == end:
        return False
    if start < end:
        return start <= minutes_now < end
    return minutes_now >= start or minutes_now < end  # 跨零点


class Subscribers:
    """推送目标清单。"""

    def __init__(self, path: str):
        self.path = path
        raw = read_json(path, {}) or {}
        items = raw.get("items") if isinstance(raw, dict) else raw
        self._items: list[dict[str, Any]] = [i for i in (items or []) if isinstance(i, dict) and i.get("umo")]

    def save(self) -> None:
        with contextlib.suppress(OSError):
            write_json(self.path, {"items": self._items})

    def all(self) -> list[dict[str, Any]]:
        return list(self._items)

    def umos(self) -> list[str]:
        return [item["umo"] for item in self._items]

    def get(self, umo: str) -> dict[str, Any] | None:
        for item in self._items:
            if item["umo"] == umo:
                return item
        return None

    def add(self, umo: str, *, label: str = "", events: list[str] | None = None) -> bool:
        if not umo:
            return False
        if self.get(umo):
            return False
        self._items.append({
            "umo": umo,
            "label": label or umo,
            "events": events,
            "quiet_hours": None,
            "added_at": _now(),
        })
        self.save()
        return True

    def remove(self, umo: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i["umo"] != umo]
        if len(self._items) != before:
            self.save()
            return True
        return False

    def set_quiet(self, umo: str, spec: str | None) -> bool:
        item = self.get(umo)
        if not item:
            return False
        item["quiet_hours"] = spec
        self.save()
        return True

    def clear(self) -> int:
        count = len(self._items)
        self._items = []
        self.save()
        return count


def _now() -> float:
    import time

    return time.time()


class Pusher:
    """把已定好的事件发到订阅会话。路由/合并都在上游完成，这里只管过滤与投递。"""

    def __init__(
        self,
        context: Any,
        subscribers: Subscribers,
        config: Callable[[str, Any], Any],
        hooks: Any = None,
        logger: Any = None,
        fallback_targets: list[str] | Callable[[], list[str]] | None = None,
    ):
        self.context = context
        self.subscribers = subscribers
        self._config = config
        self._hooks = hooks
        self.log = logger
        self._fallback = fallback_targets or []
        self._last_delivery: dict[str, Any] = {}

    # ── 目标 ────────────────────────────────────────────────
    def targets(self) -> list[str]:
        targets = self.subscribers.umos()
        extra = self._fallback() if callable(self._fallback) else self._fallback
        for umo in extra or []:
            if umo and umo not in targets:
                targets.append(umo)
        return targets

    def _quiet_blocks(self, umo: str, events: list[DeviceEvent]) -> list[DeviceEvent]:
        if not bool(self._config("quiet_hours_enabled", False)):
            item = self.subscribers.get(umo) or {}
            spec = item.get("quiet_hours")
        else:
            item = self.subscribers.get(umo) or {}
            spec = item.get("quiet_hours") or str(self._config("quiet_hours", "") or "")
        if not spec:
            return events

        import time

        minutes_now = time.localtime().tm_hour * 60 + time.localtime().tm_min
        if not in_quiet_hours(spec, minutes_now):
            return events
        # 安静时段只放 critical
        return [e for e in events if e.severity == SEV_CRITICAL]

    def _subscribe_filter(self, umo: str, events: list[DeviceEvent]) -> list[DeviceEvent]:
        item = self.subscribers.get(umo) or {}
        allow = item.get("events")
        if not allow:
            return events
        allow_set = {str(x) for x in allow}
        return [e for e in events if e.type in allow_set]

    # ── 投递 ────────────────────────────────────────────────
    async def deliver(self, events: list[DeviceEvent]) -> int:
        """返回成功投递的会话数。"""
        if not events:
            return 0
        targets = self.targets()
        if not targets:
            if self.log:
                self.log.warning("没有推送目标：请先 /banbu bind 或 /banbu subscribe")
            return 0

        sent = 0
        for umo in targets:
            selected = self._subscribe_filter(umo, events)
            selected = self._quiet_blocks(umo, selected)
            if not selected:
                continue
            text = render.render_batch(selected)
            if self._hooks:
                replaced = await self._hooks.emit_first("on_before_send", text, umo)
                if isinstance(replaced, str) and replaced.strip():
                    text = replaced
            if not text.strip():
                continue
            ok = await self._send(umo, text)
            if ok:
                sent += 1
                self._last_delivery[umo] = {"ts": _now(), "count": len(selected)}
        return sent

    async def _send(self, umo: str, text: str) -> bool:
        try:
            from astrbot.api.event import MessageChain  # 延迟导入，保证可单测
        except ImportError:
            if self.log:
                self.log.error("当前环境没有 astrbot，跳过推送")
            return False
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
            if not ok and self.log:
                self.log.warning("平台可能不支持主动消息（umo=%s）", umo)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("推送失败(%s): %s: %s", umo, type(exc).__name__, exc)
            return False

    def status(self) -> dict[str, Any]:
        return {
            "targets": len(self.targets()),
            "umos": self.targets(),
            "last_delivery": dict(self._last_delivery),
        }
