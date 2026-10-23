"""与框架无关的数据模型：快照、事件、规则、账号。

本模块**不导入 astrbot**，可直接单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

# 事件类型常量（工程内唯一来源，router/render/tools 都从这里取）
EV_PRINT_STARTED = "print_started"
EV_PRINT_PAUSED = "print_paused"
EV_PRINT_RESUMED = "print_resumed"
EV_PRINT_FINISHED = "print_finished"
EV_PRINT_FAILED = "print_failed"
EV_HMS_ERROR = "hms_error"
EV_FILAMENT_RUNOUT = "filament_runout"
EV_TEMP_ALARM = "temp_alarm"
EV_OFFLINE = "offline"
EV_ONLINE = "online"
EV_MILESTONE = "milestone"
EV_RULE_HIT = "rule_hit"
EV_TASK_SUMMARY = "task_summary"
EV_PERIODIC_DIGEST = "periodic_digest"

ALL_EVENT_TYPES = (
    EV_PRINT_STARTED,
    EV_PRINT_PAUSED,
    EV_PRINT_RESUMED,
    EV_PRINT_FINISHED,
    EV_PRINT_FAILED,
    EV_HMS_ERROR,
    EV_FILAMENT_RUNOUT,
    EV_TEMP_ALARM,
    EV_OFFLINE,
    EV_ONLINE,
    EV_MILESTONE,
    EV_RULE_HIT,
    EV_TASK_SUMMARY,
    EV_PERIODIC_DIGEST,
)

# 严重级别（决定合并排序与安静时段是否放行）
SEV_INFO = "info"
SEV_WARN = "warn"
SEV_CRITICAL = "critical"

SEVERITY_ORDER = {SEV_CRITICAL: 0, SEV_WARN: 1, SEV_INFO: 2}

# 路由档位
CH_DIRECT = "direct"
CH_LLM = "llm"
CH_BOTH = "both"


def now() -> float:
    return time.time()


def deep_merge(dst: dict, src: dict) -> dict:
    """把 src 深合并进 dst 并返回 dst（原地）。

    MQTT 增量报文只带变化字段，需要合并进上一次的全量快照。
    """
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def dig(data: Any, path: str, default: Any = None) -> Any:
    """按 "a.b.0.c" 取值，取不到返回 default。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return default if cur is None else cur


@dataclass
class Snapshot:
    """一次设备状态快照（已归一化为 canonical 字段）。"""

    device: str = "bambu_a1"
    connected: bool = False
    state_raw: str = ""
    state: str = "unknown"
    percent: int | None = None
    layer: int | None = None
    total_layers: int | None = None
    remaining_min: int | None = None
    elapsed_min: int | None = None
    total_min: int | None = None
    nozzle: float | None = None
    nozzle_target: float | None = None
    bed: float | None = None
    bed_target: float | None = None
    chamber: float | None = None
    task_name: str = ""
    task_id: str = ""
    speed: int | None = None
    light: str = ""
    wifi: str = ""
    stage: int | None = None
    print_error: int | None = None
    fans: dict[str, Any] = field(default_factory=dict)
    ams: list[dict[str, Any]] = field(default_factory=list)
    vtray: dict[str, Any] | None = None
    hms: list[Any] = field(default_factory=list)
    dry_time: int | None = None
    target_temp: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=now)

    # ── 便捷判断 ──────────────────────────────────────────────
    @property
    def is_printing(self) -> bool:
        return self.state == "printing"

    @property
    def is_paused(self) -> bool:
        return self.state == "paused"

    @property
    def is_idle(self) -> bool:
        return self.state in ("idle", "finished", "failed")

    def progress_text(self) -> str:
        if self.percent is None:
            return "未知"
        if self.layer and self.total_layers:
            return f"{self.percent}%（第 {self.layer}/{self.total_layers} 层）"
        return f"{self.percent}%"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)  # 原始报文很大，不落盘
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class DeviceEvent:
    """一条待推送的事件。channel 由 router 决定，render/bridge 只消费。"""

    type: str
    severity: str = SEV_INFO
    device: str = "bambu_a1"
    data: dict[str, Any] = field(default_factory=dict)
    rule_id: str | None = None
    channel: str = CH_DIRECT
    dedup_key: str = ""
    ts: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceEvent":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class Rule:
    """用户提醒规则。type 白名单见 rules.RULE_TYPES。"""

    id: str
    type: str
    value: float | None = None
    value2: float | None = None
    scope: str = "job"          # job | always
    cooldown_s: int = 0
    channel: str | None = None  # 覆盖 llm_route 的通道（None = 跟随配置）
    enabled: bool = True
    label: str = ""
    created_by: str = "user"
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class Account:
    """绑定信息。token 与手机号只以密文形式落盘。"""

    region: str = "China"
    uid: str = ""
    username: str = ""          # u_{uid}
    phone_masked: str = ""
    phone_cipher: dict[str, Any] = field(default_factory=dict)
    token_cipher: dict[str, Any] = field(default_factory=dict)
    serial: str = ""
    updated_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class DeviceAction:
    """对设备的控制动作（只开放安全子集）。"""

    kind: str                    # light | pause | resume | stop | dryer_start ...
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
