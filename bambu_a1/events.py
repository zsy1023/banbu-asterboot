"""事件检测：状态跃迁 / 故障码 / 断料 / 里程碑 / 离线。

纯函数 + 一个持久化去重壳（EventState），不依赖 astrbot、不发网络请求、不调 LLM，
可以用 JSON 夹具直接单测（见 tests/test_core.py）。
"""

from __future__ import annotations

import os
from typing import Any

from .compat import Profile
from .models import (
    EV_FILAMENT_RUNOUT,
    EV_HMS_ERROR,
    EV_MILESTONE,
    EV_OFFLINE,
    EV_ONLINE,
    EV_PRINT_FAILED,
    EV_PRINT_FINISHED,
    EV_PRINT_PAUSED,
    EV_PRINT_RESUMED,
    EV_PRINT_STARTED,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARN,
    DeviceEvent,
    Snapshot,
    now,
)
from .store import read_json, write_json

PRINTING_STATES_BEFORE_START = {"idle", "prepare", "finished", "failed", "unknown", ""}


def _hms_codes(snap: Snapshot) -> set[str]:
    codes: set[str] = set()
    for item in snap.hms or []:
        if isinstance(item, dict):
            code = item.get("code")
            if code:
                codes.add(str(code))
        elif item:
            codes.add(str(item))
    return codes


def _dedup(*parts: Any) -> str:
    return ":".join(str(p) for p in parts if p not in (None, ""))


def detect(
    prev: Snapshot | None,
    cur: Snapshot,
    profile: Profile,
    milestones: list[int] | None = None,
    first_poll: bool = False,
) -> list[DeviceEvent]:
    """对比新旧快照产出事件列表。

    first_poll=True 时只建立基线：不把"启动时已经处于 FINISH/FAILED/PAUSE"
    当成刚发生的事件（否则每次重启都会误报一次完成）。
    """
    events: list[DeviceEvent] = []
    if not cur or not cur.connected:
        return events

    prev_state = prev.state if prev else ""
    cur_state = cur.state
    task = cur.task_id or cur.task_name

    def push(evt: DeviceEvent) -> None:
        events.append(evt)

    # ── 状态跃迁 ────────────────────────────────────────────
    if cur_state == "printing":
        if prev is not None and prev_state == "paused":
            push(DeviceEvent(
                type=EV_PRINT_RESUMED,
                severity=SEV_INFO,
                device=cur.device,
                data={"task_name": cur.task_name, "percent": cur.percent},
                dedup_key=_dedup(EV_PRINT_RESUMED, task, int(cur.ts)),
            ))
        elif not first_poll and (prev is None or prev_state in PRINTING_STATES_BEFORE_START):
            push(DeviceEvent(
                type=EV_PRINT_STARTED,
                severity=SEV_INFO,
                device=cur.device,
                data={"task_name": cur.task_name, "total_min": cur.total_min},
                dedup_key=_dedup(EV_PRINT_STARTED, task),
            ))

    if cur_state == "paused" and prev is not None and prev_state == "printing":
        reason, reason_key, detail = profile.pause_reason(cur.raw)
        push(DeviceEvent(
            type=EV_PRINT_PAUSED,
            severity=SEV_CRITICAL if reason_key in ("filament_runout", "hms_error", "device_error") else SEV_WARN,
            device=cur.device,
            data={
                "reason": reason,
                "reason_key": reason_key,
                "task_name": cur.task_name,
                "percent": cur.percent,
                "layer": cur.layer,
                **{k: v for k, v in detail.items() if k not in ("stage",)},
            },
            dedup_key=_dedup(EV_PRINT_PAUSED, task, cur.layer, cur.percent),
        ))
        if reason_key == "filament_runout":
            push(DeviceEvent(
                type=EV_FILAMENT_RUNOUT,
                severity=SEV_CRITICAL,
                device=cur.device,
                data={"task_name": cur.task_name, "percent": cur.percent},
                dedup_key=_dedup(EV_FILAMENT_RUNOUT, task, cur.layer),
            ))

    if cur_state == "finished" and prev_state != "finished" and not first_poll:
        worked = cur.elapsed_min or cur.total_min
        push(DeviceEvent(
            type=EV_PRINT_FINISHED,
            severity=SEV_INFO,
            device=cur.device,
            data={
                "task_name": cur.task_name,
                "elapsed_min": worked,
                "total_layers": cur.total_layers,
            },
            dedup_key=_dedup(EV_PRINT_FINISHED, task),
        ))

    if cur_state == "failed" and prev_state != "failed" and not first_poll:
        push(DeviceEvent(
            type=EV_PRINT_FAILED,
            severity=SEV_CRITICAL,
            device=cur.device,
            data={
                "task_name": cur.task_name,
                "percent": cur.percent,
                "print_error": cur.print_error,
                "hms_codes": sorted(_hms_codes(cur)),
            },
            dedup_key=_dedup(EV_PRINT_FAILED, task, cur.percent),
        ))

    # ── 故障码新增 ──────────────────────────────────────────
    # 首轮基线不能把"机器上已经存在的故障码"当成刚发生（否则绑定时就报一次故障）
    cur_codes = _hms_codes(cur)
    prev_codes = _hms_codes(prev) if prev else set()
    new_codes = sorted(cur_codes - prev_codes) if (prev is not None and not first_poll) else []
    if new_codes:
        push(DeviceEvent(
            type=EV_HMS_ERROR,
            severity=SEV_CRITICAL,
            device=cur.device,
            data={"codes": new_codes, "task_name": cur.task_name, "percent": cur.percent},
            dedup_key=_dedup(EV_HMS_ERROR, ",".join(new_codes)),
        ))

    # ── 进度里程碑 ──────────────────────────────────────────
    # 三个前提，缺一不可：
    #   1. 必须正在打印 —— 已经 FINISH / PAUSE / IDLE 时"跨过 25%"没有意义，
    #      否则绑定时机器正好停在 100%，会一次性喷出 25/50/75/90 四条假进度；
    #   2. 必须是增量比较（prev 存在且 percent 已知）—— 首轮基线不能算"刚跨过"；
    #   3. first_poll 期间一律不报。
    if (
        milestones
        and cur.percent is not None
        and cur.state == "printing"
        and not first_poll
        and prev is not None
        and prev.percent is not None
    ):
        prev_percent = prev.percent
        for mark in sorted(set(milestones)):
            if prev_percent < mark <= cur.percent:
                push(DeviceEvent(
                    type=EV_MILESTONE,
                    severity=SEV_INFO,
                    device=cur.device,
                    data={
                        "percent": mark,
                        "layer": cur.layer,
                        "total_layers": cur.total_layers,
                        "remaining_min": cur.remaining_min,
                        "task_name": cur.task_name,
                    },
                    dedup_key=_dedup(EV_MILESTONE, task, mark),
                ))

    return events


def detect_offline(prev: Snapshot | None, stale_after: int) -> DeviceEvent | None:
    """长时间没有报文 → 离线事件（按状态跃迁，不重复产生）。"""
    if not prev or not prev.connected:
        return None
    idle_for = now() - prev.ts
    if idle_for < stale_after:
        return None
    return DeviceEvent(
        type=EV_OFFLINE,
        severity=SEV_CRITICAL,
        device=prev.device,
        data={"idle_s": int(idle_for)},
        dedup_key=_dedup(EV_OFFLINE, prev.task_id or ""),
    )


def online_event(cur: Snapshot, was_offline: bool) -> DeviceEvent | None:
    if not was_offline:
        return None
    return DeviceEvent(
        type=EV_ONLINE,
        severity=SEV_INFO,
        device=cur.device,
        data={"state": cur.state},
        dedup_key=_dedup(EV_ONLINE, int(cur.ts)),
    )


class EventState:
    """跨重启的检测状态：上一份快照、去重键、里程碑命中、离线标记。"""

    MAX_KEYS = 200

    def __init__(self, path: str):
        self.path = path
        data = read_json(path, {}) or {}
        self.last_snapshot: Snapshot | None = (
            Snapshot.from_dict(data["last_snapshot"]) if data.get("last_snapshot") else None
        )
        self.task_id: str = data.get("task_id", "")
        self.seen: list[str] = list(data.get("seen") or [])
        self.offline: bool = bool(data.get("offline"))
        self._seen_set = set(self.seen)

    # ── 任务基线 ────────────────────────────────────────────
    def sync_task(self, snap: Snapshot) -> bool:
        """任务变化时清空去重记录，返回是否换了任务。"""
        task = snap.task_id or snap.task_name or ""
        if task and task != self.task_id:
            self.task_id = task
            self.seen = []
            self._seen_set = set()
            return True
        return False

    def remember(self, key: str) -> bool:
        """首次见到返回 True；重复返回 False。"""
        if not key:
            return True
        if key in self._seen_set:
            return False
        self._seen_set.add(key)
        self.seen.append(key)
        if len(self.seen) > self.MAX_KEYS:
            for old in self.seen[: len(self.seen) - self.MAX_KEYS]:
                self._seen_set.discard(old)
            self.seen = self.seen[-self.MAX_KEYS:]
        return True

    def filter_new(self, events: list[DeviceEvent]) -> list[DeviceEvent]:
        return [e for e in events if self.remember(e.dedup_key)]

    # ── 持久化 ──────────────────────────────────────────────
    def save(self, snap: Snapshot | None = None) -> None:
        if snap is not None:
            self.last_snapshot = snap
        payload = {
            "last_snapshot": self.last_snapshot.to_dict() if self.last_snapshot else None,
            "task_id": self.task_id,
            "seen": self.seen[-self.MAX_KEYS:],
            "offline": self.offline,
            "saved_at": now(),
        }
        try:
            write_json(self.path, payload)
        except OSError:
            pass

    def reset(self) -> None:
        self.last_snapshot = None
        self.task_id = ""
        self.seen = []
        self._seen_set = set()
        self.offline = False


def events_log_path(data_dir: str) -> str:
    return os.path.join(data_dir, "events.jsonl")
