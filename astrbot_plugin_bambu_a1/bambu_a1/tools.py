"""自然语言入口的工具实现（被 main.py 的 @filter.llm_tool 薄封装调用）。

原则：
  - 只返回事实 + 提示，不在这里二次调用 LLM（避免双倍成本）
  - 手机号/验证码不接受参数，绑定只能走 /banbu 命令
  - stop/pause 之类危险动作要求 confirm=true 二次确认
"""

from __future__ import annotations

import json
from typing import Any

from .models import DeviceAction
from .rules import RULE_TYPES, parse_temp_range


class BambuTools:
    """把插件内部能力包装成"返回字符串"的工具集。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ── 读 ──────────────────────────────────────────────────
    def get_status(self, detail: bool = False) -> str:
        snap = self.plugin.snapshot()
        if not snap or not snap.connected:
            return json.dumps({
                "bound": self.plugin.is_bound(),
                "monitoring": self.plugin.monitor_status().get("running", False),
                "hint": "还没有状态数据。若未绑定请让用户在聊天里发送 /banbu bind <手机号>。",
            }, ensure_ascii=False)

        payload: dict[str, Any] = {
            "bound": True,
            "state": snap.state,
            "state_raw": snap.state_raw,
            "task": snap.task_name,
            "percent": snap.percent,
            "layer": snap.layer,
            "total_layers": snap.total_layers,
            "remaining_min": snap.remaining_min,
            "elapsed_min": snap.elapsed_min,
            "nozzle_c": snap.nozzle,
            "nozzle_target_c": snap.nozzle_target,
            "bed_c": snap.bed,
            "bed_target_c": snap.bed_target,
            "chamber_c": snap.chamber,
            "light": snap.light,
            "data_age_s": int(self.plugin.snapshot_age_s() or 0),
        }
        if detail:
            payload["ams"] = snap.ams
            payload["vtray"] = snap.vtray
            payload["fans"] = snap.fans
            payload["hms"] = snap.hms
        payload["hint"] = "把这些数字直接讲给用户，不要改动或推测；缺失项说未知。"
        return json.dumps(payload, ensure_ascii=False)

    def get_history(self, limit: int = 10) -> str:
        records = self.plugin.recent_events(max(1, min(int(limit or 10), 50)))
        if not records:
            return json.dumps({"events": [], "hint": "还没有历史事件"}, ensure_ascii=False)
        slim = [
            {
                "type": r.get("type"),
                "ts": r.get("ts"),
                "data": {k: v for k, v in (r.get("data") or {}).items()
                         if k in ("task_name", "percent", "reason", "codes", "label", "elapsed_min")},
            }
            for r in records
        ]
        return json.dumps({"events": slim}, ensure_ascii=False)

    def list_reminders(self) -> str:
        rules = self.plugin.rules.all()
        return json.dumps({
            "rules": [
                {
                    "id": r.id, "type": r.type, "value": r.value, "value2": r.value2,
                    "label": r.label, "channel": r.channel, "enabled": r.enabled,
                }
                for r in rules
            ],
            "available_types": list(RULE_TYPES.keys()),
        }, ensure_ascii=False)

    # ── 写 ──────────────────────────────────────────────────
    def add_reminder(
        self,
        type: str,
        value: str = "",
        value2: str = "",
        label: str = "",
        use_llm: bool = False,
    ) -> str:
        rtype = str(type or "").strip()
        num: Any = value
        num2: Any = value2 or None

        if rtype == "temp_in_range" and isinstance(value, str) and "-" in value:
            parsed = parse_temp_range(value)
            if not parsed:
                return "温度区间格式不对，用 30-40 这种写法。"
            num, num2 = parsed

        rule, error = self.plugin.rules.add(
            rtype,
            num,
            num2,
            label=label or "",
            channel="llm" if use_llm else None,
            created_by="llm",
        )
        if not rule:
            return f"没加上：{error}"
        return f"已添加规则 [{rule.id}] {rule.label}。用户下次问可以用 /banbu remind list 查看。"

    def delete_reminder(self, rule_id: str) -> str:
        ok = self.plugin.rules.delete(str(rule_id or "").strip())
        return f"已删除规则 {rule_id}" if ok else f"没找到规则 {rule_id}"

    def set_llm_pref(self, mode: str) -> str:
        mode = str(mode or "").strip().lower()
        if mode not in ("off", "summary_only", "full"):
            return "只有 off / summary_only / full 三档。"
        self.plugin.set_config("llm_mode", mode)
        return f"LLM 档位已切到 {mode}。"

    def set_route(self, event_type: str, channel: str) -> str:
        event_type = str(event_type or "").strip()
        channel = str(channel or "").strip().lower()
        if channel not in ("direct", "llm", "both"):
            return "通道只能是 direct / llm / both。"
        route = dict(self.plugin.config_get("llm_route", {}) or {})
        route[event_type] = channel
        self.plugin.set_config("llm_route", route)
        return f"{event_type} 现在走 {channel}。"

    async def control(self, action: str, mode: str = "", confirm: bool = False) -> str:
        action = str(action or "").strip().lower()
        if action not in ("light", "pause", "resume", "stop"):
            return "只支持 light / pause / resume / stop。"

        dangerous = action in ("stop", "pause")
        if dangerous and not confirm:
            token = self.plugin.request_confirm(action)
            return (
                f"{action} 会打断当前打印。确认请让用户再说一次并带上确认"
                f"（命令：/banbu {action} confirm，确认码 {token}，60 秒内有效）。"
            )

        params = {"mode": str(mode or "").lower()} if action == "light" else {}
        result = await self.plugin.execute_action(DeviceAction(kind=action, params=params))
        return result.message or ("已发送" if result.ok else "发送失败")

    # ── 引导 ────────────────────────────────────────────────
    def bind_guide(self) -> str:
        return (
            "绑定只能通过命令完成（为了不让手机号和验证码进入模型上下文）：\n"
            "1. 在聊天里发送 /banbu bind <11 位手机号>\n"
            "2. 收到短信后发送 /banbu code <验证码>\n"
            "3. 发送 /banbu sn <打印机序列号>（不知道就跳过，用 /banbu devices 试试自动列举）\n"
            "绑定成功后本会话会自动成为推送目标。"
        )
