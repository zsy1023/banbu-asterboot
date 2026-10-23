"""协议档案：端点表 / 字段映射 / 状态映射 / 漂移检测。

设计目标（见 ARCHITECTURE.md 第 9 节）：云端改字段名、改域名、加枚举时，
只改 protocol_profiles/*.json，不改 Python。本模块不依赖 astrbot。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from .models import Snapshot, dig
from .store import read_json

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protocol_profiles")


def _cast(value: Any, kind: str) -> Any:
    try:
        if kind == "int":
            return int(float(value))
        if kind == "float":
            return round(float(value), 2)
        if kind == "str":
            return str(value)
        if kind == "pct_fan":
            # 拓竹风扇档位 0-14 → 百分比
            return int(float(value)) * 100 // 14
    except (TypeError, ValueError):
        return None
    return value


def _deep_update(base: dict, patch: dict) -> dict:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


class Profile:
    """一个协议档案的运行时视图。"""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.name: str = data.get("profile", "unknown")
        # 漂移统计（进程内累计）
        self.missing_fields: dict[str, int] = {}
        self.unknown_states: dict[str, int] = {}
        self.unknown_fields: dict[str, int] = {}

    # ── 端点 ────────────────────────────────────────────────
    def endpoints(self, region: str) -> dict[str, str]:
        table = self.data.get("endpoints", {})
        chosen = table.get(region) or table.get("China") or {}
        out = {}
        api = (chosen.get("api_base") or "").rstrip("/")
        sms = (chosen.get("sms_base") or api).rstrip("/")
        out["api_base"] = api
        out["sms_base"] = sms
        out["login_url"] = api + (chosen.get("login_path") or "")
        out["preference_url"] = api + (chosen.get("preference_path") or "")
        out["sms_url"] = sms + (chosen.get("sms_path") or "")
        out["csrf_url"] = sms + (chosen.get("csrf_path") or "")
        out["bind_list_url"] = api + (chosen.get("bind_list_path") or "")
        return out

    @property
    def mqtt(self) -> dict[str, Any]:
        return self.data.get("mqtt", {})

    def mqtt_broker(self, region: str) -> str:
        mqtt = self.mqtt
        return mqtt.get("broker") if region != "Global" else (
            mqtt.get("broker_global") or mqtt.get("broker")
        )

    def mqtt_topic(self, key: str, serial: str) -> str:
        return (self.mqtt.get(key) or "").format(serial=serial)

    def mqtt_command(self, key: str) -> dict[str, Any]:
        return copy.deepcopy(self.mqtt.get(key) or {})

    # ── 字段 ────────────────────────────────────────────────
    def get(self, raw: dict[str, Any], canonical: str, fill_missing: bool = True) -> Any:
        spec = (self.data.get("fields") or {}).get(canonical)
        if not spec:
            return None
        for path in spec.get("paths") or []:
            value = dig(raw, path)
            if value is None:
                continue
            if "scale" in spec:
                try:
                    value = float(value) * float(spec["scale"])
                except (TypeError, ValueError):
                    continue
            casted = _cast(value, spec.get("cast", ""))
            if casted is not None:
                return casted
        if fill_missing:
            self.missing_fields[canonical] = self.missing_fields.get(canonical, 0) + 1
        return None

    def normalize_state(self, state_raw: str) -> str:
        states = self.data.get("states") or {}
        if state_raw in states:
            return states[state_raw]
        if state_raw:
            self.unknown_states[state_raw] = self.unknown_states.get(state_raw, 0) + 1
            return state_raw.lower()
        return "unknown"

    # ── 暂停原因 ────────────────────────────────────────────
    def pause_reason(self, raw: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """返回 (中文原因, reason_key, 附加数据)。优先级：阶段码 > HMS > print_error。"""
        labels = self.data.get("pause_reasons") or {}
        stage_map = self.data.get("stage_pause_reasons") or {}
        detail: dict[str, Any] = {}

        stage = self.get(raw, "stage", fill_missing=False)
        if stage is not None:
            detail["stage"] = stage
            key = stage_map.get(str(stage))
            if key:
                return labels.get(key, key), key, detail

        hms = dig(raw, "print.hms") or []
        if isinstance(hms, list) and hms:
            detail["hms"] = hms
            code = hms[0].get("code") if isinstance(hms[0], dict) else hms[0]
            detail["hms_code"] = code
            return labels.get("hms_error", "设备故障"), "hms_error", detail

        print_error = self.get(raw, "print_error", fill_missing=False)
        if print_error:
            detail["print_error"] = print_error
            module = (int(print_error) >> 24) & 0xFF
            detail["hms_module"] = (self.data.get("hms_modules") or {}).get(str(module), "unknown")
            return labels.get("device_error", "设备故障"), "device_error", detail

        return labels.get("unknown", "原因未知"), "unknown", detail

    # ── 快照 ────────────────────────────────────────────────
    def to_snapshot(self, raw: dict[str, Any], device: str = "bambu_a1") -> Snapshot:
        raw = raw or {}
        state_raw = self.get(raw, "state_raw") or ""
        snap = Snapshot(
            device=device,
            connected=bool(raw),
            state_raw=state_raw,
            state=self.normalize_state(state_raw),
            percent=self.get(raw, "percent"),
            layer=self.get(raw, "layer"),
            total_layers=self.get(raw, "total_layers"),
            remaining_min=self.get(raw, "remaining_min"),
            elapsed_min=self.get(raw, "elapsed_min"),
            total_min=self.get(raw, "total_min"),
            nozzle=self.get(raw, "nozzle"),
            nozzle_target=self.get(raw, "nozzle_target"),
            bed=self.get(raw, "bed"),
            bed_target=self.get(raw, "bed_target"),
            chamber=self.get(raw, "chamber"),
            task_name=self.get(raw, "task_name") or "",
            task_id=str(self.get(raw, "task_id") or ""),
            speed=self.get(raw, "speed"),
            light=self.get(raw, "light") or "",
            wifi=self.get(raw, "wifi") or "",
            stage=self.get(raw, "stage"),
            print_error=self.get(raw, "print_error"),
            fans={
                "cooling": self.get(raw, "fan_cooling"),
                "heatbreak": self.get(raw, "fan_heatbreak"),
                "big1": self.get(raw, "fan_big1"),
                "big2": self.get(raw, "fan_big2"),
            },
            ams=self.parse_ams(raw),
            vtray=self.parse_vtray(raw),
            hms=dig(raw, "print.hms") or [],
            raw=raw,
        )
        self.note_unknown_fields(raw)
        return snap

    def parse_ams(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        ams = dig(raw, "print.ams") or {}
        for unit in (ams.get("ams") or []) if isinstance(ams, dict) else []:
            trays = []
            for tray in unit.get("tray") or []:
                trays.append({
                    "id": tray.get("id"),
                    "type": tray.get("tray_type") or "未知",
                    "color": f"#{str(tray.get('tray_color') or '000000FF')[:6]}",
                    "remain": tray.get("tray_remain"),
                })
            out.append({
                "id": unit.get("id"),
                "humidity": unit.get("humidity"),
                "trays": trays,
            })
        return out

    def parse_vtray(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        vtray = dig(raw, "print.vtray") or {}
        trays = vtray.get("tray") if isinstance(vtray, dict) else None
        if not trays:
            return None
        tray = trays[0] if isinstance(trays, list) else trays
        if not isinstance(tray, dict):
            return None
        return {
            "type": tray.get("tray_type") or "外挂",
            "color": f"#{str(tray.get('tray_color') or '000000FF')[:6]}",
            "remain": tray.get("tray_remain"),
        }

    # ── 漂移 ────────────────────────────────────────────────
    def note_unknown_fields(self, raw: dict[str, Any]) -> None:
        declared = set(self.data.get("state_fields_declared") or [])
        if not declared:
            return
        print_node = dig(raw, "print") or {}
        if not isinstance(print_node, dict):
            return
        for key in print_node:
            path = f"print.{key}"
            if path in declared:
                continue
            # 声明为前缀的（如 print.ams.xxx）也算已声明
            if any(path == d or path.startswith(d + ".") for d in declared):
                continue
            self.unknown_fields[path] = self.unknown_fields.get(path, 0) + 1

    def drift_report(self) -> dict[str, Any]:
        return {
            "profile": self.name,
            "missing_fields": dict(sorted(self.missing_fields.items())),
            "unknown_states": dict(sorted(self.unknown_states.items())),
            "unknown_fields": dict(sorted(self.unknown_fields.items())),
        }

    def has_drift(self) -> bool:
        return bool(self.missing_fields or self.unknown_states or self.unknown_fields)


def load_profile(name: str = "a1_v1", overrides: dict[str, Any] | None = None) -> Profile:
    """加载档案；文件缺失时抛出，让上层的降级提示能给出可读原因。"""
    path = os.path.join(PROFILE_DIR, f"{name}.json")
    data = read_json(path, None)
    if not isinstance(data, dict):
        raise FileNotFoundError(f"协议档案不存在或损坏: {path}")
    if overrides:
        data = _deep_update(data, overrides)
    return Profile(data)


def has_capability(obj: Any, name: str) -> bool:
    """能力探测：不比较版本号，直接看宿主对象有没有这个能力。"""
    return callable(getattr(obj, name, None))


def dump_profile(profile: Profile) -> str:
    return json.dumps(profile.data, ensure_ascii=False, indent=2)
