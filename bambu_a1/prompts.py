"""LLM 提示词与防编造约束。

原则：模型只做"措辞与解释"，事实一律来自插件给的 <facts> JSON；
缺数据就写"未知"，禁止推测温度/时间/错误码。
"""

from __future__ import annotations

import json
from typing import Any

GUARDRAILS = (
    "你是 3D 打印机的状态播报助手，只负责把已有事实讲清楚。严格遵守：\n"
    "1. 只能引用 <facts> 里出现的数值，不得编造、换算或推测温度、时间、层数、错误码；\n"
    "2. 事实里没有的项写“未知”，不要一句“应该/大概”；\n"
    "3. 不要输出 JSON、代码块或表格，只用简体中文短句；\n"
    "4. 不要重复 <facts> 里已经说过的整段内容，只补充解释、归因或建议；\n"
    "5. 建议必须可执行（改什么、看哪里、下一步发什么命令）。"
)

SUMMARY_SYSTEM = GUARDRAILS + "\n本次任务：给这次打印写一段复盘，最多 120 字。"

DIAGNOSIS_SYSTEM = (
    GUARDRAILS
    + "\n本次任务：解释这次异常最可能的原因，并给出 2–3 条排查建议，最多 150 字。"
    + "\n不要下结论式断言“一定是 XX 坏了”，用“常见原因是”这类措辞。"
)

DIGEST_SYSTEM = GUARDRAILS + "\n本次任务：把这段时间的打印情况做个简短小结，最多 200 字。"

STATUS_ANSWER_SYSTEM = (
    GUARDRAILS
    + "\n本次任务：用户问打印机状态，用 1–3 句话直接回答，把关键数字说清楚（进度、剩余时间、温度）。"
)

RULE_PARSE_SYSTEM = (
    "你是提醒规则解析器。把用户的话转成 JSON，不要输出任何解释文字。\n"
    "可用字段：type, value, value2, label, channel。\n"
    "type 只能是：layer_reached, percent_reached, print_remaining, print_duration, "
    "nozzle_temp_above, nozzle_temp_below, bed_temp_above, bed_temp_below, chamber_temp_above, "
    "temp_in_range, hms_code, filament_low, offline。\n"
    "value 为数字（hms_code 为字符串）；temp_in_range 需要 value2。\n"
    "无法解析时输出 {\"error\": \"原因\"}。"
)


def facts_block(payload: dict[str, Any]) -> str:
    """把结构化事实包成 <facts> 段，状态整段注入（不写 system_prompt，避免破坏提示词缓存）。"""
    return "<facts>\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n</facts>"


def event_facts(payload: dict[str, Any]) -> str:
    return facts_block(payload)


def summary_prompt(task: dict[str, Any], history: list[dict[str, Any]] | None = None) -> str:
    payload: dict[str, Any] = {"kind": "print_finished", "task": task}
    if history:
        payload["recent_tasks"] = history[:5]
    payload["instruction"] = "写一段这次打印的复盘，最多 120 字，别重复任务名和耗时数字。"
    return facts_block(payload)


def diagnosis_prompt(event_data: dict[str, Any], recent_events: list[dict[str, Any]] | None = None) -> str:
    payload: dict[str, Any] = {"kind": "failure", "event": event_data}
    if recent_events:
        payload["recent_events"] = recent_events[:8]
    payload["instruction"] = "解释最可能的原因并给 2–3 条排查建议，最多 150 字。"
    return facts_block(payload)


def digest_prompt(stats: dict[str, Any]) -> str:
    payload = {"kind": "digest", "stats": stats}
    payload["instruction"] = "做一段打印情况小结，最多 200 字。"
    return facts_block(payload)


def status_answer_prompt(snapshot: dict[str, Any], question: str = "") -> str:
    payload = {"kind": "status", "snapshot": snapshot, "question": question or "现在打印得怎么样"}
    payload["instruction"] = "用 1–3 句话回答，关键数字必须来自 snapshot。"
    return facts_block(payload)


def rule_parse_prompt(text: str, known_types: list[str]) -> str:
    payload = {"utterance": text, "types": known_types}
    return facts_block(payload) + "\n请输出 JSON。"
