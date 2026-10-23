"""用户提醒规则引擎。

规则是**数据**（JSON），这里是唯一的判定实现：新增类型只改本文件，
不需要动 events/monitor/router。类型白名单与 ARCHITECTURE.md 第 7 节一致。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from .models import (
    EV_OFFLINE,
    EV_RULE_HIT,
    EV_TEMP_ALARM,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARN,
    DeviceEvent,
    Rule,
    Snapshot,
)
from .store import read_json, write_json

MAX_RULES = 50

# id → (中文名, 需要 value, 需要 value2, 取值范围)
RULE_TYPES: dict[str, tuple[str, bool, bool, tuple[float, float] | None]] = {
    "layer_reached":      ("到达指定层", True, False, (1, 100000)),
    "percent_reached":    ("进度达到百分比", True, False, (1, 100)),
    "print_remaining":    ("剩余时间低于（分钟）", True, False, (1, 100000)),
    "print_duration":     ("已打印超过（分钟）", True, False, (1, 100000)),
    "nozzle_temp_above":  ("喷头温度超过（°C）", True, False, (0, 400)),
    "nozzle_temp_below":  ("喷头温度低于（°C）", True, False, (0, 400)),
    "bed_temp_above":     ("热床温度超过（°C）", True, False, (0, 200)),
    "bed_temp_below":     ("热床温度低于（°C）", True, False, (0, 200)),
    "chamber_temp_above": ("腔温超过（°C）", True, False, (0, 120)),
    "temp_in_range":      ("温度进入区间（°C）", True, True, (0, 400)),
    "hms_code":           ("出现指定故障码", True, False, None),
    "filament_low":       ("AMS 余料低于（%）", True, False, (0, 100)),
    "offline":            ("离线超过（分钟）", True, False, (1, 1440)),
}

# 温度类规则产出 temp_alarm 事件，其余产出 rule_hit
TEMP_RULE_TYPES = {
    "nozzle_temp_above", "nozzle_temp_below",
    "bed_temp_above", "bed_temp_below",
    "chamber_temp_above", "temp_in_range",
}


def parse_temp_range(text: str) -> tuple[float, float] | None:
    """把 "30-40" 解析成 (30.0, 40.0)。"""
    text = str(text or "").replace("~", "-").replace("，", "-").strip()
    if "-" not in text:
        return None
    left, _, right = text.partition("-")
    try:
        low, high = float(left), float(right)
    except ValueError:
        return None
    return (low, high) if low <= high else (high, low)


class RuleEngine:
    """规则的增删改查 + 命中判定。"""

    def __init__(self, path: str):
        self.path = path
        self._rules: list[Rule] = [
            Rule.from_dict(item) for item in (read_json(path, []) or [])
            if isinstance(item, dict) and item.get("type") in RULE_TYPES
        ]

    # ── 持久化 ──────────────────────────────────────────────
    def save(self) -> None:
        try:
            write_json(self.path, [r.to_dict() for r in self._rules])
        except OSError:
            pass

    def all(self) -> list[Rule]:
        return list(self._rules)

    def enabled(self) -> list[Rule]:
        return [r for r in self._rules if r.enabled]

    # ── 校验 ────────────────────────────────────────────────
    @staticmethod
    def validate(
        rtype: str,
        value: Any,
        value2: Any = None,
    ) -> tuple[bool, str, float | None, float | None]:
        if rtype not in RULE_TYPES:
            available = "、".join(RULE_TYPES.keys())
            return False, f"不支持的规则类型 {rtype}。可用：{available}", None, None
        label, need_value, need_value2, bounds = RULE_TYPES[rtype]

        num: float | None = None
        if need_value:
            if isinstance(value, str) and rtype == "hms_code":
                return True, "", None, None
            try:
                num = float(value)
            except (TypeError, ValueError):
                return False, f"{label} 需要一个数字参数", None, None
            if bounds and not (bounds[0] <= num <= bounds[1]):
                return False, f"{label} 的取值需在 {bounds[0]}~{bounds[1]} 之间", None, None

        num2: float | None = None
        if need_value2:
            try:
                num2 = float(value2)
            except (TypeError, ValueError):
                return False, f"{label} 需要区间上限", None, None
            if num is not None and num2 <= num:
                return False, "温度区间上限必须大于下限", None, None
        return True, "", num, num2

    # ── 增删 ────────────────────────────────────────────────
    def add(
        self,
        rtype: str,
        value: Any,
        value2: Any = None,
        label: str = "",
        channel: str | None = None,
        created_by: str = "user",
        cooldown_s: int = 0,
    ) -> tuple[Rule | None, str]:
        if len(self._rules) >= MAX_RULES:
            return None, f"规则数量已达上限 {MAX_RULES} 条，请先删除一些"
        ok, error, num, num2 = self.validate(rtype, value, value2)
        if not ok:
            return None, error
        rule = Rule(
            id=f"r_{uuid.uuid4().hex[:6]}",
            type=rtype,
            value=num if num is not None else (str(value) if rtype == "hms_code" else None),
            value2=num2,
            label=label or RULE_TYPES[rtype][0],
            channel=channel,
            created_by=created_by,
            cooldown_s=cooldown_s,
        )
        self._rules.append(rule)
        self.save()
        return rule, ""

    def delete(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        if len(self._rules) != before:
            self.save()
            return True
        return False

    def clear(self) -> int:
        count = len(self._rules)
        self._rules = []
        self.save()
        return count

    def toggle(self, rule_id: str, enabled: bool | None = None) -> Rule | None:
        for rule in self._rules:
            if rule.id == rule_id:
                rule.enabled = (not rule.enabled) if enabled is None else enabled
                self.save()
                return rule
        return None

    # ── 判定 ────────────────────────────────────────────────
    def evaluate(self, prev: Snapshot | None, cur: Snapshot) -> list[DeviceEvent]:
        """返回命中的规则事件（不含去重，去重由 EventState 做）。"""
        hits: list[DeviceEvent] = []
        if not cur or not cur.connected:
            return hits

        for rule in self.enabled():
            data = self._match(rule, prev, cur)
            if data is None:
                continue
            evt_type = EV_TEMP_ALARM if rule.type in TEMP_RULE_TYPES else EV_RULE_HIT
            severity = self._severity(rule, evt_type)
            hits.append(DeviceEvent(
                type=evt_type,
                severity=severity,
                device=cur.device,
                data={"rule_id": rule.id, "rule_type": rule.type, "label": rule.label, **data},
                rule_id=rule.id,
                dedup_key=f"{evt_type}:{rule.id}:{cur.task_id or cur.task_name}:{data.get('stamp', '')}",
            ))
        return hits

    @staticmethod
    def _severity(rule: Rule, evt_type: str) -> str:
        if evt_type == EV_TEMP_ALARM:
            return SEV_CRITICAL
        if rule.type in ("offline", "filament_low"):
            return SEV_WARN
        return SEV_INFO

    def _match(self, rule: Rule, prev: Snapshot | None, cur: Snapshot) -> dict[str, Any] | None:
        rtype = rule.type
        value = rule.value

        if prev is None:
            return None

        if rtype == "layer_reached":
            old = prev.layer or 0
            new = cur.layer or 0
            if old < int(value) <= new:
                return {"layer": new, "total_layers": cur.total_layers, "stamp": int(value)}
            return None

        if rtype == "percent_reached":
            old = prev.percent or 0
            new = cur.percent or 0
            if old < int(value) <= new:
                return {"percent": new, "stamp": int(value)}
            return None

        if rtype == "print_remaining":
            old = prev.remaining_min
            new = cur.remaining_min
            if old is None or new is None:
                return None
            if old > int(value) >= new:
                return {"remaining_min": new, "stamp": int(value)}
            return None

        if rtype == "print_duration":
            old = prev.elapsed_min or 0
            new = cur.elapsed_min or 0
            if old < int(value) <= new:
                return {"elapsed_min": new, "stamp": int(value)}
            return None

        if rtype in TEMP_RULE_TYPES:
            return self._match_temp(rule, prev, cur)

        if rtype == "hms_code":
            codes_now = {
                str(item.get("code")) if isinstance(item, dict) else str(item)
                for item in (cur.hms or [])
            }
            codes_before = {
                str(item.get("code")) if isinstance(item, dict) else str(item)
                for item in (prev.hms or [])
            }
            target = str(value or "").strip()
            if not target:
                return None
            hit = {c for c in codes_now - codes_before if c == target or c.startswith(target)}
            if hit:
                return {"codes": sorted(hit), "stamp": target}
            return None

        if rtype == "filament_low":
            low = []
            for unit in cur.ams:
                for tray in unit.get("trays") or []:
                    remain = tray.get("remain")
                    if isinstance(remain, (int, float)) and remain < float(value):
                        low.append({
                            "unit": unit.get("id"),
                            "tray": tray.get("id"),
                            "type": tray.get("type"),
                            "remain": remain,
                        })
            if low:
                return {"trays": low, "stamp": min(int(t["remain"]) for t in low)}
            return None

        if rtype == "offline":
            if cur.connected:
                return None
            idle_min = (time.time() - prev.ts) / 60
            if idle_min >= float(value):
                return {"idle_min": round(idle_min, 1), "stamp": int(idle_min)}
            return None

        return None

    def _match_temp(self, rule: Rule, prev: Snapshot, cur: Snapshot) -> dict[str, Any] | None:
        rtype = rule.type
        value = rule.value
        pick = {
            "nozzle_temp_above": ("nozzle", "喷头"),
            "nozzle_temp_below": ("nozzle", "喷头"),
            "bed_temp_above": ("bed", "热床"),
            "bed_temp_below": ("bed", "热床"),
            "chamber_temp_above": ("chamber", "腔体"),
            "temp_in_range": ("bed", "热床"),
        }.get(rtype)
        if not pick:
            return None
        attr, name = pick
        old_t = getattr(prev, attr)
        new_t = getattr(cur, attr)
        if old_t is None or new_t is None:
            return None

        if rtype == "nozzle_temp_above" or rtype == "bed_temp_above" or rtype == "chamber_temp_above":
            if old_t <= float(value) < new_t:
                return {"component": name, "temp": new_t, "threshold": value, "stamp": int(value)}
            return None
        if rtype == "nozzle_temp_below" or rtype == "bed_temp_below":
            if old_t >= float(value) > new_t:
                return {"component": name, "temp": new_t, "threshold": value, "stamp": int(value)}
            return None
        if rtype == "temp_in_range":
            high = rule.value2 if rule.value2 is not None else float(value) + 10
            outside_before = old_t < float(value) or old_t > high
            inside_now = float(value) <= new_t <= high
            if outside_before and inside_now:
                return {
                    "component": name,
                    "temp": new_t,
                    "range": [value, high],
                    "stamp": int(value),
                }
        return None


def default_rules_path(data_dir: str) -> str:
    return os.path.join(data_dir, "rules.json")
