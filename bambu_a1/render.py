"""确定性文本渲染：纯模板，零 LLM、零网络。

样式（0.1.6 起，卡片式排版，**不使用 emoji**）：

    ▸ 拓竹 A1 · 开始打印
    ━━━━━━━━━━━━━━━━━━━━
    任务　多功能PCB测试工作台_万向蛇…
    预计　4 小时 22 分

  - 标题行：`几何字形 + 设备 · 事件`，字形全部取自几何/制表符区段（▸ ‖ ✓ ✕ ◆ ▲ ⊘ ◉ ▰），
    不是 emoji，各客户端都按普通字符渲染
  - 标题下一条 `━` 制表线，形成"卡片头部"
  - 细节行：`标签 + 全角空格对齐 + 值`，2 字标签自动补全角空格，和 3 字标签对齐
  - 页脚（操作提示）与正文之间空一行
  - 事实与模型补写用 `━━━━` 分隔

所有数值都来自结构化事实；缺失就省略整行，绝不显示 0 假装正常。
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

RULE = "━" * 20
SEP = f"\n\n{RULE}\n\n"
DIVIDER = RULE
TASK_NAME_MAX = 16
LABEL_PAD = "　"  # 全角空格：让 2 字标签与 3 字标签视觉对齐
TEMP_BAR_WIDTH = 8

# 状态卡顶部的像素图：**只画热床和上面正在成型的件**。
# 不画机身，就不必纠结 bedslinger / CoreXY —— 机型结构跟这条消息无关。
#
# 只用方块字符、**不用空格**：U+2588/U+2584 这类方块在中文客户端里常被按全角渲染，
# 一旦用半角空格补齐宽度，整张图就会歪。全方块 + 靠行宽差表达层次，
# 在"全角方块"和"半角方块"两种渲染下都保持相对形状。
#   最窄的一行是顶层，最宽的一行是底层，最后一行是热床
_ART_BED = "▄" * 10
_ART_LAYERS = (
    "████",        # 第 3 层（最窄）
    "██████",      # 第 2 层
    "████████",    # 第 1 层（贴着热床）
)  # 由窄到宽，像逐层堆起来

#: 兼容旧引用：满进度（100%）时的样子
PRINTER_ART = (_ART_LAYERS[0], _ART_LAYERS[1], _ART_LAYERS[2], _ART_BED)


def art_layer_count(percent: float | None) -> int:
    """把进度映射成"已经堆了几层"（0–3）。0 或未知 → 只有热床。"""
    if percent is None:
        return 0
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if value >= 100:
        return 3
    return max(1, min(3, 1 + int(value // 34)))  # 1–33% → 1 层，34–67% → 2 层，68%+ → 3 层


def printer_art(percent: float | None = None) -> tuple[str, ...]:
    """热床 + 成型中的件：层数随进度增长，空层直接不画（不留空行）。"""
    filled = art_layer_count(percent)
    rows = [_ART_LAYERS[index] for index in range(3 - filled, 3)]
    rows.append(_ART_BED)
    return tuple(rows)


#: 状态卡底部的"下一步"提示（能做什么，比单纯报状态有用）
STATE_HINT = {
    "paused": "发送 /banbu resume 继续打印",
    "failed": "发 /banbu protocol 看诊断，/banbu digest 看最近事件",
    "offline": "检查供电与网络，恢复后会自动通知",
}

#: 这些状态下进度为 0 没有信息量，状态卡里省略该行
_HIDE_ZERO_PROGRESS_STATES = {"idle", "finished", "failed", "unknown"}

# 事件标题前的几何字形。刻意避开 Unicode emoji 区段：
#   ▸(25B8) ‖(2016) ✓(2713) ✕(2715) ◆(25C6) ▲(25B2) ⊘(2298) ◉(25C9) ▰(25B0)
GLYPH = {
    "print_started": "▸",
    "print_resumed": "▸",
    "print_paused": "‖",
    "print_finished": "✓",
    "print_failed": "✕",
    "hms_error": "◆",
    "filament_runout": "◆",
    "temp_alarm": "▲",
    "offline": "⊘",
    "online": "◉",
    "milestone": "▰",
    "rule_hit": "◆",
    "task_summary": "▪",
    "periodic_digest": "▪",
}

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
    """进度条带端点，像仪表：`▏███░░░░░░░░▕ 25%`。"""
    if percent is None:
        return "▏" + "░" * width + "▕"
    filled = max(0, min(width, round(float(percent) / 100 * width)))
    return "▏" + "█" * filled + "░" * (width - filled) + "▕"


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


def temp_bar(current: float | None, target: float | None, width: int = TEMP_BAR_WIDTH) -> str | None:
    """温度小进度条：▰▰▰▰▰▰▱▱ 215/215°C。

    - 有目标温度：按 current/target 填充（超过目标按满格算，正在升温时是半格）
    - 无目标温度（如腔体）：只显示数值，不画没有意义的空条
    """
    if current is None:
        return None
    if not target or target <= 0:
        return f"{current:.0f}°C"
    ratio = max(0.0, min(1.0, float(current) / float(target)))
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {current:.0f}/{target:.0f}°C"


def kv(label: str, value: Any) -> str | None:
    """`标签 + 全角空格 + 值`；值为空则整行省略。

    统一用全角空格分隔（不再按标签长度切换），避免 2 字/3 字标签混排时列不齐的观感。
    """
    if value is None or value == "":
        return None
    return f"{label}{LABEL_PAD}{value}"


def card_head(device_name: str, headline: str, event_type: str | None = None) -> list[str]:
    """卡片头：`▸ 拓竹 A1 · 事件` + 制表线。"""
    glyph = GLYPH.get(event_type or "")
    head = f"{device_name} · {headline}"
    return [f"{glyph} {head}" if glyph else head, RULE]


def _rows(pairs: list[tuple[str, Any]], footer: str | None = None) -> list[str]:
    lines: list[str] = []
    for label, value in pairs:
        line = kv(label, value)
        if line:
            lines.append(line)
    if footer:
        lines.append("")
        lines.append(footer)
    return lines


# ── 各事件模板 ──────────────────────────────────────────
def _event_lines(event: DeviceEvent, device_name: str) -> list[str]:
    data = event.data or {}

    if event.type == EV_PRINT_STARTED:
        return card_head(device_name, "开始打印", event.type) + _rows([
            ("任务", truncate(data.get("task_name"))),
            ("预计", fmt_minutes(data["total_min"]) if data.get("total_min") else None),
        ])

    if event.type == EV_PRINT_PAUSED:
        percent = data.get("percent")
        where = None
        if percent is not None:
            where = f"{percent}%"
            if data.get("layer"):
                where += f" · 第 {data['layer']} 层"
        return card_head(device_name, "打印暂停", event.type) + _rows(
            [
                ("原因", data.get("reason")),
                ("进度", where),
            ],
            footer="换好料后发送 /banbu resume 继续",
        )

    if event.type == EV_PRINT_RESUMED:
        return card_head(device_name, "继续打印", event.type) + _rows([
            ("任务", truncate(data.get("task_name"))),
        ])

    if event.type == EV_PRINT_FINISHED:
        if data.get("inferred"):
            # 我们没观察到 FINISH（轮询错过了），按"进度归零 + 换了任务"推断出来的：
            # 结论是这一件结束了，但耗时不可信，所以不编造。
            return card_head(device_name, "打印结束", event.type) + _rows([
                ("任务", truncate(data.get("task_name"))),
                ("进度", f"{data['percent']}%" if data.get("percent") is not None else None),
                ("说明", "没抓到完成状态，按进度归零推断"),
            ])
        return card_head(device_name, "打印完成", event.type) + _rows([
            ("任务", truncate(data.get("task_name"))),
            ("耗时", fmt_minutes(data["elapsed_min"]) if data.get("elapsed_min") else None),
        ])

    if event.type == EV_PRINT_FAILED:
        percent = data.get("percent")
        codes = data.get("hms_codes") or []
        raw_error = data.get("print_error")
        try:
            error_text = f"0x{int(raw_error):04X}" if raw_error else None
        except (TypeError, ValueError):
            error_text = str(raw_error) if raw_error else None
        return card_head(device_name, "打印失败", event.type) + _rows([
            ("任务", truncate(data.get("task_name"))),
            ("进度", f"{percent}%" if percent is not None else None),
            ("错误码", error_text),
            ("故障码", " ".join(str(c) for c in codes) if codes else None),
        ])

    if event.type == EV_HMS_ERROR:
        codes = " ".join(str(c) for c in data.get("codes") or [])
        percent = data.get("percent")
        return card_head(device_name, "设备报故障", event.type) + _rows([
            ("故障码", codes or None),
            ("进度", f"{percent}%" if percent is not None else None),
        ])

    if event.type == EV_FILAMENT_RUNOUT:
        percent = data.get("percent")
        return card_head(device_name, "耗材用尽（断料）", event.type) + _rows(
            [
                ("任务", truncate(data.get("task_name"))),
                ("进度", f"{percent}%" if percent is not None else None),
            ],
            footer="换好料后发送 /banbu resume 继续",
        )

    if event.type == EV_TEMP_ALARM:
        component = data.get("component") or "温度"
        if data.get("range"):
            low, high = data["range"]
            value = f"{data.get('temp')}°C（区间 {low}~{high}°C）"
        else:
            value = f"{data.get('temp')}°C（阈值 {data.get('threshold')}°C）"
        return card_head(device_name, "温度告警", event.type) + _rows([(component, value)])

    if event.type == EV_OFFLINE:
        idle = data.get("idle_s")
        return card_head(device_name, "离线", event.type) + _rows(
            [("时长", f"已 {fmt_minutes(idle / 60)} 没有数据" if idle else "长时间没有数据")],
            footer="检查供电与网络，恢复后会自动通知",
        )

    if event.type == EV_ONLINE:
        state = STATE_LABEL.get(data.get("state") or "", data.get("state") or "")
        return card_head(device_name, "恢复在线", event.type) + _rows([("当前", state or None)])

    if event.type == EV_MILESTONE:
        percent = data.get("percent")
        layer, total = data.get("layer"), data.get("total_layers")
        remaining = data.get("remaining_min")
        return card_head(device_name, f"进度 {percent}%", event.type) + _rows([
            ("层次", f"{layer}/{total} 层" if layer and total else (f"第 {layer} 层" if layer else None)),
            ("剩余", fmt_minutes(remaining) if isinstance(remaining, (int, float)) and remaining > 0 else None),
            ("任务", truncate(data.get("task_name"))),
        ])

    if event.type == EV_RULE_HIT:
        label = data.get("label") or "提醒"
        bits: list[str] = []
        if data.get("percent") is not None:
            bits.append(f"进度 {data['percent']}%")
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
        return card_head(device_name, label, event.type) + _rows([("当前", " · ".join(bits) or None)])

    if event.type in (EV_TASK_SUMMARY, EV_PERIODIC_DIGEST):
        text = data.get("text")
        return [str(text)] if text else []

    # 未知类型：至少把事实摊开，不静默
    extra = " · ".join(
        f"{key} {value}" for key, value in data.items()
        if not key.startswith("_") and key != "llm_text"
    )
    return card_head(device_name, event.type) + _rows([("详情", extra or None)])


# ── 对外接口 ────────────────────────────────────────────
def render_event(event: DeviceEvent, *, device_name: str = "拓竹 A1") -> str:
    return "\n".join(_event_lines(event, device_name)).strip()


def render_batch(events: list[DeviceEvent], *, device_name: str = "拓竹 A1") -> str:
    """把一批事件渲染成一条消息；模型补写接在事实之后，用一条制表线隔开。"""
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
        return "\n".join(
            card_head(device_name, "暂无数据") + ["还没拿到过状态，先确认绑定与监控是否正常。"]
        )

    lines = card_head(device_name, STATE_LABEL.get(snap.state, snap.state))
    # 小打印机放在卡片头下面，作为视觉标识（纯方块字符，不含 CJK）
    lines.extend(printer_art(snap.percent))
    lines.append("")

    def add(label: str, value: Any) -> None:
        line = kv(label, value)
        if line:
            lines.append(line)

    add("任务", truncate(snap.task_name, 24) if snap.task_name else None)
    show_progress = snap.percent is not None and not (
        snap.percent == 0 and snap.state in _HIDE_ZERO_PROGRESS_STATES
    )
    if show_progress:
        add("进度", f"{progress_bar(snap.percent)} {snap.percent}%")
    if snap.layer:
        total = f"/{snap.total_layers}" if snap.total_layers else ""
        add("层次", f"{snap.layer}{total}")

    time_bits = []
    # 0 / 负数没有信息量：FINISH 时 mc_remaining_time 就是 0，显示"剩 0 分钟"会误导
    if snap.elapsed_min and snap.elapsed_min > 0:
        time_bits.append(f"已 {fmt_minutes(snap.elapsed_min)}")
    if snap.remaining_min and snap.remaining_min > 0:
        time_bits.append(f"剩 {fmt_minutes(snap.remaining_min)}")
    if time_bits:
        add("时间", " · ".join(time_bits))

    # 喷头/热床各一条小进度条，腔体没有目标温度就只给数值
    add("喷头", temp_bar(snap.nozzle, snap.nozzle_target))
    add("热床", temp_bar(snap.bed, snap.bed_target))
    if snap.chamber is not None:
        add("腔体", f"{snap.chamber:.0f}°C")

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
        add("耗材", " · ".join(trays[:4]))

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
        add("设备", " · ".join(bits))

    for line in extra_lines or []:
        if line:
            lines.append(line)

    # "下一步能做什么"：比单纯报状态更有用
    hint = STATE_HINT.get(snap.state)
    if hint:
        lines.append("")
        lines.append(hint)

    # 只去掉尾部空行；中间的空白（像素图后那一行）要保留
    while lines and not lines[-1]:
        lines.pop()
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
    """不用 emoji 的严重度标记（供需要前缀的调用方使用）。"""
    if event.severity == SEV_CRITICAL:
        return "[严重]"
    return "[提醒]" if event.severity == "warn" else "[信息]"
