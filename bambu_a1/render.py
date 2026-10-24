"""确定性文本渲染：纯模板，零 LLM、零网络。

样式约定（0.1.5 起）：
  - **不使用 emoji/彩色图标**，全部用排版表达层次：标题行 `设备 · 事件`，
    细节行 `标签 值`，多条信息用 ` · ` 分隔，长任务名截断到 16 个字符 + …
  - 进度条用 `█`/`░`，分隔线用 `─`；这些是几何字符，不是 emoji，各家客户端都能正常显示
  - 所有数值都来自结构化事实；缺失就省略整行，绝不显示 0 假装正常
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

SEP = "\n\n──────────\n\n"
DIVIDER = "──────────"
TASK_NAME_MAX = 16

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


# ── 基础排版 ────────────────────────────────────────────
def fmt_minutes(minutes: float | int | None) -> str:
    """分钟 → "3 小时 54 分" / "42 分钟"。中文口径，不用 h/m 缩写。"""
    if minutes is None:
        return "未知"
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return "未知"
    if total <= 0:
        return "0 分钟"
    if total < 60:
        return f"{total} 分钟"
    hours, mins = divmod(total, 60)
    return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"


def truncate(text: str | None, limit: int = TASK_NAME_MAX) -> str:
    """任务名太长就截断，避免一条消息被文件名淹没。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


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


def title(device_name: str, headline: str) -> str:
    """统一标题行：`拓竹 A1 · 打印完成`。"""
    return f"{device_name} · {headline}"


def kv(label: str, value: Any) -> str | None:
    """`标签 值`；值为空则整行省略。"""
    if value is None or value == "":
        return None
    return f"{label} {value}"


def _lines(*candidates: str | None) -> list[str]:
    return [line for line in candidates if line]


def _event_lines(event: DeviceEvent, device_name: str) -> list[str]:
    data = event.data or {}

    if event.type == EV_PRINT_STARTED:
        return _lines(
            title(device_name, "开始打印"),
            truncate(data.get("task_name")),
            kv("预计", fmt_minutes(data["total_min"]) if data.get("total_min") else None),
        )

    if event.type == EV_PRINT_PAUSED:
        percent = data.get("percent")
        where = None
        if percent is not None:
            where = f"进度 {percent}%"
            if data.get("layer"):
                where += f" · 第 {data['layer']} 层"
        return _lines(
            title(device_name, "打印暂停"),
            kv("原因", data.get("reason")),
            where,
            "换好料后发送 /banbu resume 继续",
        )

    if event.type == EV_PRINT_RESUMED:
        return _lines(title(device_name, "继续打印"), truncate(data.get("task_name")))

    if event.type == EV_PRINT_FINISHED:
        return _lines(
            title(device_name, "打印完成"),
            truncate(data.get("task_name")),
            kv("耗时", fmt_minutes(data["elapsed_min"]) if data.get("elapsed_min") else None),
        )

    if event.type == EV_PRINT_FAILED:
        percent = data.get("percent")
        return _lines(
            title(device_name, "打印失败"),
            truncate(data.get("task_name")),
            percent if percent is None else f"进度 {percent}%",
            kv("错误码", data.get("print_error")),
            kv("故障码", " ".join(str(c) for c in data["hms_codes"]) if data.get("hms_codes") else None),
        )

    if event.type == EV_HMS_ERROR:
        codes = " ".join(str(c) for c in data.get("codes") or [])
        percent = data.get("percent")
        return _lines(
            title(device_name, "设备报故障"),
            kv("故障码", codes or None),
            percent if percent is None else f"进度 {percent}%",
        )

    if event.type == EV_FILAMENT_RUNOUT:
        percent = data.get("percent")
        detail = truncate(data.get("task_name")) if data.get("task_name") else ""
        if percent is not None:
            detail = f"{detail} {percent}%".strip()
        return _lines(
            title(device_name, "耗材用尽（断料）"),
            detail or None,
            "换好料后发送 /banbu resume 继续",
        )

    if event.type == EV_TEMP_ALARM:
        component = data.get("component") or "温度"
        if data.get("range"):
            low, high = data["range"]
            line = f"{component} {data.get('temp')}°C（区间 {low}~{high}°C）"
        else:
            line = f"{component} {data.get('temp')}°C（阈值 {data.get('threshold')}°C）"
        return _lines(title(device_name, "温度告警"), line)

    if event.type == EV_OFFLINE:
        idle = data.get("idle_s")
        since = f"已 {fmt_minutes(idle / 60)} 没有数据" if idle else "长时间没有数据"
        return _lines(
            title(device_name, "离线"),
            since,
            "检查供电与网络，恢复后会自动通知",
        )

    if event.type == EV_ONLINE:
        state = STATE_LABEL.get(data.get("state") or "", data.get("state") or "")
        return _lines(title(device_name, "恢复在线"), kv("当前", state or None))

    if event.type == EV_MILESTONE:
        percent = data.get("percent")
        detail = None
        layer, total = data.get("layer"), data.get("total_layers")
        if layer:
            detail = f"{layer}/{total} 层" if total else f"第 {layer} 层"
        remaining = data.get("remaining_min")
        if isinstance(remaining, (int, float)) and remaining > 0:
            remain_text = f"剩 {fmt_minutes(remaining)}"
            detail = f"{detail} · {remain_text}" if detail else remain_text
        return _lines(
            title(device_name, f"进度 {percent}%"),
            detail,
            truncate(data.get("task_name")),
        )

    if event.type == EV_RULE_HIT:
        label = data.get("label") or "提醒"
        percent = data.get("percent")
        bits: list[str] = []
        if percent is not None:
            bits.append(f"进度 {percent}%")
        if data.get("layer"):
            bits.append(f"第 {data['layer']} 层")
        if data.get("remaining_min") is not None:
            bits.append(f"剩 {fmt_minutes(data['remaining_min'])}")
        if data.get("elapsed_min") is not None:
            bits.append(f"已打印 {fmt_minutes(data['elapsed_min'])}")
        if data.get("temp") is not None:
            bits.append(f"{data.get('component') or ''} {data['temp']}°C".strip())
        if data.get("codes"):
            bits.append("故障码 " + " ".join(str(c) for c in data["codes"]))
        if data.get("trays"):
            bits.append("余料 " + " ".join(
                f"{t.get('type')} {t.get('remain')}%" for t in data["trays"]
            ))
        return _lines(title(device_name, label), " · ".join(bits) or None)

    if event.type in (EV_TASK_SUMMARY, EV_PERIODIC_DIGEST):
        text = data.get("text")
        return [str(text)] if text else []

    # 未知类型：至少把事实摊开，不静默
    extra = " · ".join(
        f"{key} {value}" for key, value in data.items()
        if not key.startswith("_") and key != "llm_text"
    )
    return _lines(title(device_name, event.type), extra or None)


# ── 对外接口 ────────────────────────────────────────────
def render_event(event: DeviceEvent, *, device_name: str = "拓竹 A1") -> str:
    return "\n".join(_event_lines(event, device_name)).strip()


def render_batch(events: list[DeviceEvent], *, device_name: str = "拓竹 A1") -> str:
    """把一批事件渲染成一条消息；模型补写接在事实之后，用一条分隔线隔开。"""
    facts: list[str] = []
    drafts: list[str] = []
    for event in events:
        text = render_event(event, device_name=device_name)
        if text:
            facts.append(text)
        draft = (event.data or {}).get("llm_text")
        if draft:
            drafts.append(str(draft).strip())
    body = "\n\n".join(facts)
    if drafts:
        body = (body + SEP + "\n\n".join(drafts)) if body else "\n\n".join(drafts)
    return body.strip()


def render_status(
    snap: Snapshot,
    *,
    state_age_s: int | None = None,
    device_name: str = "拓竹 A1",
    extra_lines: list[str] | None = None,
) -> str:
    if not snap.connected:
        return (
            f"{title(device_name, '暂无数据')}\n"
            "还没拿到过状态，先确认绑定与监控是否正常。"
        )

    lines = [title(device_name, STATE_LABEL.get(snap.state, snap.state))]
    if snap.task_name:
        lines.append(truncate(snap.task_name, 24))

    if snap.percent is not None:
        lines.append(f"进度 {progress_bar(snap.percent)} {snap.percent}%")
    if snap.layer:
        total = f"/{snap.total_layers}" if snap.total_layers else ""
        lines.append(f"层次 {snap.layer}{total}")

    time_bits = []
    # 0 / 负数没有信息量：FINISH 时 mc_remaining_time 就是 0，显示"剩 0 分钟"会误导
    if snap.elapsed_min and snap.elapsed_min > 0:
        time_bits.append(f"已 {fmt_minutes(snap.elapsed_min)}")
    if snap.remaining_min and snap.remaining_min > 0:
        time_bits.append(f"剩 {fmt_minutes(snap.remaining_min)}")
    if time_bits:
        lines.append(" · ".join(time_bits))

    temp_line = fmt_temp([
        ("喷头", snap.nozzle, snap.nozzle_target),
        ("热床", snap.bed, snap.bed_target),
        ("腔体", snap.chamber, None),
    ])
    if temp_line:
        lines.append(temp_line)

    trays = []
    for unit in snap.ams:
        for tray in unit.get("trays") or []:
            remain = tray.get("remain")
            bit = f"{tray.get('type')}"
            if remain is not None:
                bit += f" {remain}%"
            trays.append(bit)
    if snap.vtray:
        bit = f"外挂 {snap.vtray.get('type')}"
        if snap.vtray.get("remain") is not None:
            bit += f" {snap.vtray['remain']}%"
        trays.append(bit)
    if trays:
        lines.append("耗材 " + " · ".join(trays[:4]))

    bits = []
    if snap.light:
        bits.append(f"灯 {'开' if snap.light == 'on' else '关'}")
    if snap.wifi:
        bits.append(f"信号 {snap.wifi}")
    if snap.speed:
        bits.append(f"速度 {snap.speed}%")
    if state_age_s is not None:
        bits.append(f"{state_age_s} 秒前更新")
    if bits:
        lines.append(" · ".join(bits))

    for line in extra_lines or []:
        lines.append(line)
    return "\n".join(lines)


def render_rule_list(rules: list[Any]) -> str:
    if not rules:
        return (
            "还没有自定义提醒规则。\n"
            "用 /banbu remind add <类型> <值> 添加，例如 /banbu remind add layer_reached 50"
        )
    lines = ["当前提醒规则："]
    for rule in rules:
        flag = "开" if getattr(rule, "enabled", True) else "停"
        value = getattr(rule, "value", None)
        extra = f" {getattr(rule, 'value2', '')}" if getattr(rule, "value2", None) else ""
        channel = getattr(rule, "channel", None) or "跟随配置"
        lines.append(f"[{flag}] {rule.id}  {rule.label}（{rule.type}={value}{extra}，{channel}）")
    return "\n".join(lines)


def severity_badge(event: DeviceEvent) -> str:
    """不用 emoji 的严重度标记，保留给需要前缀的调用方。"""
    if event.severity == SEV_CRITICAL:
        return "[严重]"
    return "[提醒]" if event.severity == "warn" else "[信息]"
