"""确定性文本渲染：纯模板，零 LLM、零网络。

所有数值都来自结构化事实；缺失就省略该行或写"未知"，绝不显示 0 假装正常。
"""

from __future__ import annotations

from typing import Any

from .models import (
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
    SEV_CRITICAL,
    DeviceEvent,
    Snapshot,
)

SEP = "\n———\n"
STATE_LABEL = {
    "idle": "空闲",
    "prepare": "准备中",
    "printing": "打印中",
    "paused": "已暂停",
    "finished": "已完成",
    "failed": "失败",
    "slicing": "切片中",
    "unknown": "未知",
}


def fmt_minutes(minutes: float | int | None) -> str:
    if minutes is None:
        return "未知"
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return "未知"
    if total < 60:
        return f"{total} 分钟"
    hours, mins = divmod(total, 60)
    return f"{hours}h {mins:02d}m" if mins else f"{hours}h"


def progress_bar(percent: int | float | None, width: int = 12) -> str:
    if percent is None:
        return "░" * width
    filled = max(0, min(width, round(float(percent) / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def fmt_temp(pairs: list[tuple[str, float | None, float | None]]) -> str:
    parts = []
    for name, current, target in pairs:
        if current is None:
            continue
        if target:
            parts.append(f"{name} {current:.0f}/{target:.0f}°C")
        else:
            parts.append(f"{name} {current:.0f}°C")
    return " · ".join(parts)


def _device_title(snap_or_event: Any, default: str = "拓竹 A1") -> str:
    return getattr(snap_or_event, "data", {}).get("device_name") or default


# ── 状态卡 ──────────────────────────────────────────────
def render_status(snap: Snapshot, *, state_age_s: int | None = None, device_name: str = "拓竹 A1",
                  extra_lines: list[str] | None = None) -> str:
    if not snap.connected:
        return f"🖨️ {device_name} · 暂无数据\n还没拿到过状态，先确认绑定与监控是否正常。"

    lines = [f"🖨️ {device_name} · {STATE_LABEL.get(snap.state, snap.state)}"]
    if snap.task_name:
        lines.append(f"任务：{snap.task_name}")

    if snap.percent is not None:
        lines.append(f"进度：{progress_bar(snap.percent)} {snap.percent}%")
    if snap.layer:
        total = f"/{snap.total_layers}" if snap.total_layers else ""
        lines.append(f"层次：{snap.layer}{total}")

    time_bits = []
    # 0 / 负数没有信息量：FINISH 时 mc_remaining_time 就是 0，显示"剩 0 分钟"会误导
    if snap.elapsed_min and snap.elapsed_min > 0:
        time_bits.append(f"已 {fmt_minutes(snap.elapsed_min)}")
    if snap.remaining_min and snap.remaining_min > 0:
        time_bits.append(f"剩 {fmt_minutes(snap.remaining_min)}")
    if time_bits:
        lines.append("时间：" + " · ".join(time_bits))

    temp_line = fmt_temp([
        ("喷头", snap.nozzle, snap.nozzle_target),
        ("热床", snap.bed, snap.bed_target),
        ("腔体", snap.chamber, None),
    ])
    if temp_line:
        lines.append("温度：" + temp_line)

    trays = []
    for unit in snap.ams:
        for tray in unit.get("trays") or []:
            remain = tray.get("remain")
            bit = f"AMS{unit.get('id')}-{tray.get('id')} {tray.get('type')}"
            if remain is not None:
                bit += f" {remain}%"
            trays.append(bit)
    if snap.vtray:
        bit = f"外挂 {snap.vtray.get('type')}"
        if snap.vtray.get("remain") is not None:
            bit += f" {snap.vtray['remain']}%"
        trays.append(bit)
    if trays:
        lines.append("耗材：" + " · ".join(trays[:4]))

    bits = []
    if snap.light:
        bits.append(f"灯光 {snap.light}")
    if snap.wifi:
        bits.append(f"WiFi {snap.wifi}")
    if snap.speed:
        bits.append(f"速度 {snap.speed}%")
    if state_age_s is not None:
        bits.append(f"{state_age_s} 秒前更新")
    if bits:
        lines.append("设备：" + " · ".join(bits))

    for line in extra_lines or []:
        lines.append(line)
    return "\n".join(lines)


# ── 事件 ────────────────────────────────────────────────
def render_event(event: DeviceEvent, *, device_name: str = "拓竹 A1") -> str:
    data = event.data or {}
    head = f" · {device_name}"

    if event.type == EV_PRINT_STARTED:
        task = data.get("task_name") or "未知任务"
        total = fmt_minutes(data.get("total_min"))
        suffix = f" · 预计 {total}" if data.get("total_min") else ""
        return f"▶️ 开始打印{head}\n任务：{task}{suffix}"

    if event.type == EV_PRINT_PAUSED:
        reason = data.get("reason") or "原因未知"
        lines = [f"⏸️ 打印暂停{head}", f"原因：{reason}"]
        if data.get("percent") is not None:
            lines.append(f"进度：{data['percent']}%" + (f"（第 {data['layer']} 层）" if data.get("layer") else ""))
        lines.append("回复 /banbu resume 继续")
        return "\n".join(lines)

    if event.type == EV_PRINT_RESUMED:
        return f"▶️ 打印恢复{head}" + (f"\n任务：{data['task_name']}" if data.get("task_name") else "")

    if event.type == EV_PRINT_FINISHED:
        lines = [f"✅ 打印完成{head}", f"任务：{data.get('task_name') or '未知任务'}"]
        worked = fmt_minutes(data.get("elapsed_min"))
        if data.get("elapsed_min"):
            lines.append(f"耗时：{worked}")
        return "\n".join(lines)

    if event.type == EV_PRINT_FAILED:
        lines = [f"❌ 打印失败{head}", f"任务：{data.get('task_name') or '未知任务'}"]
        if data.get("percent") is not None:
            lines.append(f"进度：{data['percent']}%")
        if data.get("print_error"):
            lines.append(f"错误码：{data['print_error']}")
        if data.get("hms_codes"):
            lines.append("故障码：" + "、".join(str(c) for c in data["hms_codes"]))
        return "\n".join(lines)

    if event.type == EV_HMS_ERROR:
        codes = "、".join(str(c) for c in data.get("codes") or [])
        lines = [f"🛠️ 设备报故障{head}", f"故障码：{codes or '未知'}"]
        if data.get("percent") is not None:
            lines.append(f"进度：{data['percent']}%")
        return "\n".join(lines)

    if event.type == EV_FILAMENT_RUNOUT:
        lines = [f"🧵 耗材用尽（断料）{head}"]
        if data.get("task_name"):
            lines.append(f"任务：{data['task_name']}")
        if data.get("percent") is not None:
            lines.append(f"进度：{data['percent']}%")
        lines.append("换好料后回复 /banbu resume 继续")
        return "\n".join(lines)

    if event.type == EV_TEMP_ALARM:
        component = data.get("component") or "温度"
        if data.get("range"):
            low, high = data["range"]
            return f"🌡️ 温度进入区间{head}\n{component}：{data.get('temp')}°C（{low}~{high}°C）"
        threshold = data.get("threshold")
        return f"🌡️ 温度告警{head}\n{component}：{data.get('temp')}°C（阈值 {threshold}°C）"

    if event.type == EV_OFFLINE:
        idle = data.get("idle_s")
        since = f"已 {fmt_minutes(idle / 60 if idle else None)} 没有数据" if idle else "长时间没有数据"
        return f"📴 打印机离线{head}\n{since}\n检查打印机电量和网络，恢复后会自动通知。"

    if event.type == EV_ONLINE:
        state = STATE_LABEL.get(data.get("state") or "", data.get("state") or "")
        return f"📶 打印机恢复在线{head}\n当前状态：{state}"

    if event.type == EV_MILESTONE:
        lines = [f"📈 进度 {data.get('percent')}%{head}"]
        if data.get("layer"):
            total = f"/{data['total_layers']}" if data.get("total_layers") else ""
            lines.append(f"层次：{data['layer']}{total}")
        remaining = data.get("remaining_min")
        if isinstance(remaining, (int, float)) and remaining > 0:
            lines.append(f"剩余：{fmt_minutes(remaining)}")
        if data.get("task_name"):
            lines.append(f"任务：{data['task_name']}")
        return "\n".join(lines)

    if event.type == EV_RULE_HIT:
        label = data.get("label") or "提醒规则命中"
        lines = [f"🔔 {label}{head}"]
        if data.get("percent") is not None:
            lines.append(f"进度：{data['percent']}%")
        if data.get("layer"):
            lines.append(f"层次：{data['layer']}")
        if data.get("remaining_min") is not None:
            lines.append(f"剩余：{fmt_minutes(data['remaining_min'])}")
        if data.get("elapsed_min") is not None:
            lines.append(f"已打印：{fmt_minutes(data['elapsed_min'])}")
        if data.get("temp") is not None:
            lines.append(f"温度：{data.get('component') or ''} {data['temp']}°C")
        if data.get("codes"):
            lines.append("故障码：" + "、".join(str(c) for c in data["codes"]))
        if data.get("trays"):
            lines.append("余料：" + "、".join(
                f"{t.get('type')} {t.get('remain')}%" for t in data["trays"]
            ))
        return "\n".join(lines)

    if event.type in (EV_TASK_SUMMARY, EV_PERIODIC_DIGEST):
        return data.get("text") or ""

    # 未知类型：至少把事实打出来，不要静默
    return f"ℹ️ {event.type}{head}\n" + "\n".join(
        f"{k}：{v}" for k, v in data.items() if not k.startswith("_") and k != "llm_text"
    )


def render_batch(events: list[DeviceEvent], *, device_name: str = "拓竹 A1") -> str:
    """把一批事件渲染成一条消息；模型补写接在事实之后。"""
    facts: list[str] = []
    drafts: list[str] = []
    for event in events:
        text = render_event(event, device_name=device_name).strip()
        if text:
            facts.append(text)
        draft = (event.data or {}).get("llm_text")
        if draft:
            drafts.append(str(draft).strip())
    body = "\n\n".join(facts)
    if drafts:
        body = (body + SEP + "\n\n".join(drafts)) if body else "\n\n".join(drafts)
    return body.strip()


def render_rule_list(rules: list[Any]) -> str:
    if not rules:
        return "还没有自定义提醒规则。\n用 /banbu remind add <类型> <值> 添加，例如：/banbu remind add layer_reached 50"
    lines = ["当前提醒规则："]
    for rule in rules:
        flag = "✅" if getattr(rule, "enabled", True) else "⏸️"
        value = getattr(rule, "value", None)
        extra = f" {getattr(rule, 'value2', '')}" if getattr(rule, "value2", None) else ""
        channel = getattr(rule, "channel", None) or "跟随配置"
        lines.append(
            f"{flag} [{rule.id}] {rule.label}（{rule.type}={value}{extra}，{channel}）"
        )
    return "\n".join(lines)


def severity_badge(event: DeviceEvent) -> str:
    return "🔴" if event.severity == SEV_CRITICAL else ("🟡" if event.severity == "warn" else "🔵")
