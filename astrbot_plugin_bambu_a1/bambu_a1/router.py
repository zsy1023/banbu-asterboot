"""路由：决定一条事件走 direct / llm / both。

优先级（ARCHITECTURE.md 6.3）：单规则 rule.channel > 配置 llm_route > 事件内置默认。
外加一个总闸：llm_mode=off 时 llm/both 全部降级 direct（配置保留不清空）。
"""

from __future__ import annotations

from typing import Any

from .models import (
    CH_BOTH,
    CH_DIRECT,
    CH_LLM,
    EV_FILAMENT_RUNOUT,
    EV_HMS_ERROR,
    EV_MILESTONE,
    EV_OFFLINE,
    EV_ONLINE,
    EV_PERIODIC_DIGEST,
    EV_PRINT_FAILED,
    EV_PRINT_FINISHED,
    EV_PRINT_PAUSED,
    EV_PRINT_RESUMED,
    EV_PRINT_STARTED,
    EV_RULE_HIT,
    EV_TASK_SUMMARY,
    EV_TEMP_ALARM,
    DeviceEvent,
)

# 出厂默认分界线（WebUI 可逐项覆盖）
DEFAULT_LLM_ROUTE: dict[str, str] = {
    EV_PRINT_STARTED: CH_DIRECT,
    EV_PRINT_PAUSED: CH_DIRECT,
    EV_PRINT_RESUMED: CH_DIRECT,
    EV_PRINT_FINISHED: CH_LLM,
    EV_PRINT_FAILED: CH_LLM,
    EV_HMS_ERROR: CH_LLM,
    EV_FILAMENT_RUNOUT: CH_DIRECT,
    EV_TEMP_ALARM: CH_DIRECT,
    EV_OFFLINE: CH_DIRECT,
    EV_ONLINE: CH_DIRECT,
    EV_MILESTONE: CH_DIRECT,
    EV_RULE_HIT: CH_DIRECT,
    EV_TASK_SUMMARY: CH_LLM,
    EV_PERIODIC_DIGEST: CH_LLM,
}

# llm_mode=summary_only 时允许走模型的类型（完成复盘；问答走工具，不在此列）
SUMMARY_ONLY_ALLOWED = {EV_PRINT_FINISHED, EV_TASK_SUMMARY}

VALID_CHANNELS = {CH_DIRECT, CH_LLM, CH_BOTH}


def normalize_mode(mode: Any) -> str:
    mode = str(mode or "").strip().lower()
    return mode if mode in ("off", "summary_only", "full") else "summary_only"


def normalize_route(raw: Any) -> dict[str, str]:
    """把配置里的 llm_route 与内置默认合并，非法值忽略。"""
    route = dict(DEFAULT_LLM_ROUTE)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and str(value).lower() in VALID_CHANNELS:
                route[key] = str(value).lower()
    return route


def resolve(
    event: DeviceEvent,
    *,
    llm_route: Any = None,
    llm_mode: Any = "summary_only",
    rule_channel: str | None = None,
) -> str:
    """算出这条事件的通道。"""
    mode = normalize_mode(llm_mode)
    if mode == "off":
        return CH_DIRECT

    channel = CH_DIRECT
    rule = (rule_channel or event.data.get("channel") or "").lower()
    if rule in VALID_CHANNELS:
        channel = rule
    else:
        route = normalize_route(llm_route)
        channel = route.get(event.type, CH_DIRECT)

    if channel == CH_DIRECT:
        return CH_DIRECT

    if mode == "summary_only" and event.type not in SUMMARY_ONLY_ALLOWED:
        return CH_DIRECT

    return channel


def apply(events: list[DeviceEvent], *, llm_route: Any, llm_mode: Any) -> list[DeviceEvent]:
    for event in events:
        event.channel = resolve(event, llm_route=llm_route, llm_mode=llm_mode)
    return events


def describe(llm_route: Any = None, llm_mode: Any = "summary_only") -> list[tuple[str, str, str]]:
    """给 /banbu route list 用的三列：(事件, 配置值, 当前实际生效值)。"""
    route = normalize_route(llm_route)
    mode = normalize_mode(llm_mode)
    out: list[tuple[str, str, str]] = []
    for event_type in sorted(route):
        configured = route[event_type]
        if mode == "off":
            effective = CH_DIRECT
        elif mode == "summary_only" and configured != CH_DIRECT and event_type not in SUMMARY_ONLY_ALLOWED:
            effective = CH_DIRECT
        else:
            effective = configured
        out.append((event_type, configured, effective))
    return out
