"""插件级冒烟测试：用 stub 宿主验证加载、注册、绑定链路、事件→推送链路。

不联网、不需要真的 AstrBot；它验证的是"装配是否正确"，
真正的云端/MQTT 行为仍需在 AstrBot 里实测。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_STUBS = os.path.join(_HERE, "stubs")
for path in (_STUBS, _ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot.api.event import (  # noqa: E402
    COMMANDS,
    GROUPS,
    TOOLS,
    AstrMessageEvent,
    MessageChain,
)
from astrbot.api.star import Context  # noqa: E402

from bambu_a1 import auth  # noqa: E402
from bambu_a1.models import DeviceEvent  # noqa: E402


def load_schema_defaults() -> dict:
    schema_path = os.path.join(_ROOT, "_conf_schema.json")
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    config: dict = {}
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        items = meta.get("items")
        if meta.get("type") == "object" and isinstance(items, dict):
            config[key] = {k: v.get("default") for k, v in items.items()}
        else:
            config[key] = meta.get("default")
    return config


def load_plugin_module():
    """按"顶层模块"方式加载 main.py，同时覆盖插件自带的导入回退分支。"""
    spec = importlib.util.spec_from_file_location("bambu_plugin_main", os.path.join(_ROOT, "main.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["bambu_plugin_main"] = module
    spec.loader.exec_module(module)
    return module


async def collect(agen) -> str:
    """既支持 async generator（yield），也支持直接 return 字符串。"""
    if hasattr(agen, "__aiter__"):
        parts = []
        async for item in agen:
            parts.append(getattr(item, "text", str(item)))
        return "\n".join(parts)
    result = await agen
    return getattr(result, "text", str(result or ""))


class PluginSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA_DIR"] = self.tmp.name
        self.module = load_plugin_module()
        self.context = Context()
        self.config = load_schema_defaults()
        self.plugin = self.module.BambuA1Plugin(self.context, self.config)

        # 不让监控真的去连 MQTT
        async def _fake_start():
            self.plugin.monitor._running = True

        async def _fake_refresh():
            return False

        self.plugin.monitor.start = _fake_start
        self.plugin.monitor.refresh_now = _fake_refresh

        # 云端接口打桩
        self._orig_auth = (auth.send_sms, auth.login_with_code, auth.fetch_uid, auth.list_bound_devices)
        auth.send_sms = lambda *a, **k: {"ok": True, "status": 200, "error": ""}
        auth.login_with_code = lambda *a, **k: {"ok": True, "token": "TOKEN-SECRET-XYZ", "error": ""}
        auth.fetch_uid = lambda *a, **k: {"ok": True, "uid": "42", "username": "u_42", "error": ""}
        auth.list_bound_devices = lambda *a, **k: {
            "ok": True,
            "devices": [{"serial": "01P00A12345678", "name": "A1", "model": "A1"}],
            "error": "",
        }
        self.event = AstrMessageEvent("/banbu", umo="stub:FriendMessage:1001")

    async def asyncTearDown(self):
        auth.send_sms, auth.login_with_code, auth.fetch_uid, auth.list_bound_devices = self._orig_auth
        await self.plugin.terminate()
        self.tmp.cleanup()
        os.environ.pop("ASTRBOT_STUB_DATA_DIR", None)

    async def run_cmd(self, name: str, message_str: str = "") -> str:
        """模拟 AstrBot 的调用方式：命令词剥离后按位置参数传给 handler。"""
        group = GROUPS["banbu"]
        handler = group.commands.get(name)
        self.assertIsNotNone(handler, f"命令 {name} 没注册")
        self.event.message_str = message_str or f"/banbu {name}"

        tokens = [
            token for token in self.plugin._args_after(self.event, "banbu", name)
            if not token.startswith("--")
        ]
        code = handler.__code__
        positional = list(code.co_varnames[2 : code.co_argcount])
        kwargs = dict(zip(positional, tokens))
        return await collect(handler(self.plugin, self.event, **kwargs))

    # ── 注册面 ──────────────────────────────────────────────
    async def test_command_group_registered(self):
        self.assertIn("banbu", GROUPS)
        expected = {
            "bind", "code", "sn", "devices", "unbind", "status", "monitor", "light",
            "resume", "pause", "stop", "remind", "quiet", "milestones", "subscribe",
            "unsubscribe", "llm", "route", "digest", "protocol", "test-event", "device",
        }
        self.assertTrue(expected.issubset(set(GROUPS["banbu"].commands)))

    async def test_llm_tools_registered(self):
        expected = {
            "bambu_get_status", "bambu_get_history", "bambu_list_reminders",
            "bambu_add_reminder", "bambu_delete_reminder", "bambu_set_llm_pref",
            "bambu_set_route", "bambu_control", "bambu_bind_guide",
        }
        self.assertTrue(expected.issubset(set(TOOLS)))

    async def test_llm_tool_docstrings_are_parsable(self):
        """AstrBot 靠 docstring 的 Args: 段生成 schema，这里做静态校验。"""
        for name, fn in TOOLS.items():
            doc = fn.__doc__ or ""
            self.assertTrue(doc.strip(), f"{name} 缺少 docstring")
            params = [
                p for p in fn.__code__.co_varnames[: fn.__code__.co_argcount]
                if p not in ("self", "event")
            ]
            if not params:
                continue
            self.assertIn("Args:", doc, f"{name} 缺少 Args: 段")
            for param in params:
                self.assertIn(f"{param}(", doc, f"{name} 的 Args 里没有 {param}(类型)")

    async def test_group_help(self):
        text = await collect(GROUPS["banbu"].fn(self.plugin, self.event))
        self.assertIn("/banbu bind", text)
        self.assertIn("/banbu remind", text)

    # ── 未绑定时的提示 ───────────────────────────────────────
    async def test_status_before_binding(self):
        text = await self.run_cmd("status")
        self.assertIn("还没绑定", text)

    async def test_bind_rejects_bad_phone(self):
        text = await self.run_cmd("bind", "/banbu bind 138****1111")
        self.assertIn("手机号", text)

    # ── 绑定链路（含加密落盘）────────────────────────────────
    async def test_bind_flow_encrypts_token(self):
        text = await self.run_cmd("bind", "/banbu bind 13800001111")
        self.assertIn("验证码已发送", text)

        text = await self.run_cmd("code", "/banbu code 123456")
        self.assertIn("绑定", text)
        self.assertTrue(self.plugin.is_bound() is False)  # 还没有序列号
        self.assertTrue(self.config.get("secret_key"), "应自动生成并回写 secret_key")

        account_path = os.path.join(self.plugin.data_dir, "account.json")
        raw = open(account_path, "r", encoding="utf-8").read()
        self.assertNotIn("TOKEN-SECRET-XYZ", raw, "token 不能明文落盘")
        self.assertNotIn("13800001111", raw, "手机号不能明文落盘")
        self.assertIn("138****1111", raw)

        text = await self.run_cmd("sn", "/banbu sn 01P00A12345678")
        self.assertIn("设备已绑定", text)
        self.assertTrue(self.plugin.is_bound())
        # 绑定会话自动成为推送目标
        self.assertIn("stub:FriendMessage:1001", self.plugin.subscribers.umos())

    async def test_unbind_requires_confirm(self):
        text = await self.run_cmd("unbind", "/banbu unbind")
        self.assertIn("confirm", text)
        # 先完整绑定，确认解绑真的把凭据与缓存都清掉（回归：曾因缓存残留导致仍显示已绑定）
        await self.run_cmd("bind", "/banbu bind 13800001111")
        await self.run_cmd("code", "/banbu code 123456")
        await self.run_cmd("sn", "/banbu sn 01P00A12345678")
        self.assertTrue(self.plugin.is_bound())

        text = await self.run_cmd("unbind", "/banbu unbind confirm")
        self.assertIn("已清除", text)
        self.assertFalse(self.plugin.is_bound(), "解绑后 is_bound() 必须为 False")
        self.assertEqual(self.plugin.subscribers.umos(), [])

        account_path = os.path.join(self.plugin.data_dir, "account.json")
        self.assertEqual(json.loads(open(account_path, encoding="utf-8").read()), {})
        # 再读一次也不能"复活"
        fresh = self.module.BambuA1Plugin(self.context, self.config)
        self.assertFalse(fresh.is_bound())

    async def test_devices_listing(self):
        await self.run_cmd("bind", "/banbu bind 13800001111")
        await self.run_cmd("code", "/banbu code 123456")
        text = await self.run_cmd("devices", "/banbu devices")
        self.assertIn("01P00A12345678", text)

    # ── 规则与路由命令 ──────────────────────────────────────
    async def test_remind_add_list_delete(self):
        text = await self.run_cmd("remind", "/banbu remind add layer_reached 50")
        self.assertIn("已添加", text)
        self.assertEqual(len(self.plugin.rules.all()), 1)

        text = await self.run_cmd("remind", "/banbu remind list")
        self.assertIn("layer_reached", text)

        rule_id = self.plugin.rules.all()[0].id
        text = await self.run_cmd("remind", f"/banbu remind del {rule_id}")
        self.assertIn("已删除", text)
        self.assertEqual(len(self.plugin.rules.all()), 0)

    async def test_remind_rejects_unknown_type(self):
        text = await self.run_cmd("remind", "/banbu remind add nope 1")
        self.assertIn("没加上", text)

    async def test_route_list_and_set(self):
        text = await self.run_cmd("route", "/banbu route list")
        self.assertIn("print_finished", text)

        text = await self.run_cmd("route", "/banbu route set print_paused llm")
        self.assertIn("print_paused", text)
        self.assertEqual(self.config["llm_route"].get("print_paused"), "llm")

    async def test_llm_mode_switch(self):
        text = await self.run_cmd("llm", "/banbu llm full")
        self.assertIn("full", text)
        self.assertEqual(self.config["llm_mode"], "full")

    async def test_quiet_hours_toggle(self):
        text = await self.run_cmd("quiet", "/banbu quiet 23:00-07:30")
        self.assertIn("23:00-07:30", text)
        self.assertTrue(self.config["quiet_hours_enabled"])
        text = await self.run_cmd("quiet", "/banbu quiet off")
        self.assertFalse(self.config["quiet_hours_enabled"])

    async def test_milestones_default_off(self):
        """默认不主动推进度（用户没要求就别推）。"""
        self.assertEqual(self.config["progress_milestones"], [])
        text = await self.run_cmd("milestones")
        self.assertIn("未启用", text)

    async def test_milestones_set_and_off(self):
        text = await self.run_cmd("milestones", "/banbu milestones 25,50,75")
        self.assertIn("25%", text)
        self.assertEqual(self.config["progress_milestones"], [25, 50, 75])

        text = await self.run_cmd("milestones", "/banbu milestones off")
        self.assertIn("已关闭", text)
        self.assertEqual(self.config["progress_milestones"], [])

    async def test_milestones_rejects_bad_input(self):
        text = await self.run_cmd("milestones", "/banbu milestones abc")
        self.assertIn("不是数字", text)
        text = await self.run_cmd("milestones", "/banbu milestones 120")
        self.assertIn("1", text)
        self.assertEqual(self.config["progress_milestones"], [])

    async def test_milestones_accepts_chinese_separators(self):
        await self.run_cmd("milestones", "/banbu milestones 10、20、30")
        self.assertEqual(self.config["progress_milestones"], [10, 20, 30])

    async def test_protocol_diagnostics(self):
        text = await self.run_cmd("protocol")
        self.assertIn("协议档案", text)

    # ── 事件 → 路由 → 合并 → 推送 ──────────────────────────
    async def _bind_for_push(self):
        await self.run_cmd("bind", "/banbu bind 13800001111")
        await self.run_cmd("code", "/banbu code 123456")
        await self.run_cmd("sn", "/banbu sn 01P00A12345678")
        self.plugin.config["merge_window_s"] = 0
        self.plugin.config["merge_critical_window_s"] = 0

    async def test_direct_push_without_llm(self):
        await self._bind_for_push()
        self.plugin.config["llm_mode"] = "off"
        await self.plugin._on_events(
            [DeviceEvent(type="print_finished", data={"task_name": "benchy", "elapsed_min": 100})],
            None,
        )
        await asyncio.sleep(0.05)
        self.assertTrue(self.context.sent, "应该有消息发出")
        umo, chain = self.context.sent[-1]
        self.assertEqual(umo, "stub:FriendMessage:1001")
        self.assertIsInstance(chain, MessageChain)
        text = chain.get_plain_text()
        self.assertIn("打印完成", text)
        self.assertIn("benchy", text)
        self.assertFalse(self.context.llm_calls, "llm_mode=off 时不应调用模型")

    async def test_llm_merged_into_single_message(self):
        await self._bind_for_push()
        self.plugin.config["llm_mode"] = "full"
        self.plugin.config["merge_window_s"] = 0.2
        await self.plugin._on_events(
            [DeviceEvent(type="print_finished", data={"task_name": "benchy", "elapsed_min": 100})],
            None,
        )
        await asyncio.sleep(0.5)
        self.assertTrue(self.context.llm_calls, "full 档应调用模型")
        self.assertEqual(len(self.context.sent), 1, "事实与模型文案必须合并成一条")
        text = self.context.sent[0][1].get_plain_text()
        self.assertIn("打印完成", text)
        self.assertIn(self.context.llm_reply, text)

    async def test_critical_event_reaches_push_even_if_llm_slow(self):
        await self._bind_for_push()

        async def slow_generate(**kwargs):
            await asyncio.sleep(3)
            raise AssertionError("不应该等到模型")

        self.context.llm_generate = slow_generate
        self.plugin.config["llm_mode"] = "full"
        self.plugin.config["merge_window_s"] = 0.05
        self.plugin.config["merge_critical_window_s"] = 0.05
        await self.plugin._on_events(
            [DeviceEvent(type="print_failed", severity="critical", data={"task_name": "benchy"})],
            None,
        )
        await asyncio.sleep(0.3)
        self.assertTrue(self.context.sent, "模型慢也必须把告警发出去")
        self.assertIn("打印失败", self.context.sent[0][1].get_plain_text())

    async def test_no_subscriber_means_no_push(self):
        self.plugin.config["merge_window_s"] = 0
        await self.plugin._on_events([DeviceEvent(type="print_started", data={})], None)
        await asyncio.sleep(0.05)
        self.assertFalse(self.context.sent)

    async def test_test_event_command_runs_pipeline(self):
        await self._bind_for_push()
        self.plugin.config["llm_mode"] = "off"
        text = await self.run_cmd("test-event", "/banbu test-event print_finished")
        self.assertIn("通道", text)
        await asyncio.sleep(0.05)
        self.assertTrue(self.context.sent)

    # ── 自然语言工具 ────────────────────────────────────────
    async def test_tool_get_status_before_binding(self):
        result = await TOOLS["bambu_get_status"](self.plugin, self.event)
        payload = json.loads(result)
        self.assertFalse(payload["bound"])
        self.assertIn("hint", payload)

    async def test_tool_add_and_list_reminders(self):
        result = await TOOLS["bambu_add_reminder"](
            self.plugin, self.event, type="percent_reached", value="50"
        )
        self.assertIn("已添加", result)
        listing = json.loads(await TOOLS["bambu_list_reminders"](self.plugin, self.event))
        self.assertEqual(len(listing["rules"]), 1)
        self.assertIn("percent_reached", listing["available_types"])

    async def test_tool_set_route_and_pref(self):
        await TOOLS["bambu_set_route"](self.plugin, self.event, event_type="print_failed", channel="direct")
        self.assertEqual(self.config["llm_route"]["print_failed"], "direct")
        result = await TOOLS["bambu_set_llm_pref"](self.plugin, self.event, mode="off")
        self.assertIn("off", result)
        self.assertEqual(self.config["llm_mode"], "off")

    async def test_tool_control_requires_confirm_for_stop(self):
        result = await TOOLS["bambu_control"](self.plugin, self.event, action="stop", confirm=False)
        self.assertIn("确认", result)

    async def test_tool_bind_guide_has_no_credential_params(self):
        result = await TOOLS["bambu_bind_guide"](self.plugin, self.event)
        self.assertIn("/banbu bind", result)
        self.assertIn("/banbu code", result)

    # ── 控制类命令 ──────────────────────────────────────────
    async def _stub_adapter(self):
        from bambu_a1.models import ActionResult

        calls = []

        async def fake_execute(action):
            calls.append(action)
            return ActionResult(ok=True, message=f"{action.kind} ok")

        async def fake_execute_fail(action):
            calls.append(action)
            return ActionResult(ok=False, message="MQTT 通道不可用")

        self.plugin.adapter.execute = fake_execute
        self._adapter_calls = calls
        self._adapter_fail = fake_execute_fail

    async def test_light_toggles(self):
        await self._stub_adapter()
        text = await self.run_cmd("light", "/banbu light on")
        self.assertIn("灯光", text)
        self.assertEqual(self._adapter_calls[0].kind, "light")
        self.assertEqual(self._adapter_calls[0].params["mode"], "on")

    async def test_light_without_arg_toggles_from_state(self):
        await self._stub_adapter()
        self.plugin.event_state.last_snapshot = self.module.Snapshot(connected=True, light="on")
        await self.run_cmd("light", "/banbu light")
        self.assertEqual(self._adapter_calls[0].params["mode"], "off")

    async def test_resume_and_pause_confirm(self):
        await self._stub_adapter()
        text = await self.run_cmd("resume", "/banbu resume")
        self.assertIn("继续", text)
        self.assertEqual(self._adapter_calls[0].kind, "resume")

        text = await self.run_cmd("pause", "/banbu pause")
        self.assertIn("confirm", text)
        self.assertEqual(len(self._adapter_calls), 1, "未确认时不能真的发指令")

        text = await self.run_cmd("pause", "/banbu pause confirm")
        self.assertIn("暂停", text)
        self.assertEqual(self._adapter_calls[1].kind, "pause")

    async def test_action_failure_is_reported(self):
        await self._stub_adapter()
        self.plugin.adapter.execute = self._adapter_fail
        text = await self.run_cmd("resume", "/banbu resume")
        self.assertIn("失败", text)

    # ── 其他命令 ────────────────────────────────────────────
    async def test_monitor_status_and_mode(self):
        text = await self.run_cmd("monitor", "/banbu monitor status")
        self.assertIn("监控状态", text)
        self.assertIn("推送目标", text)

        text = await self.run_cmd("monitor", "/banbu monitor poll")
        self.assertIn("poll", text)
        self.assertEqual(self.config["monitor_mode"], "poll")

    async def test_digest_without_history(self):
        text = await self.run_cmd("digest")
        self.assertIn("还没有事件记录", text)

    async def test_digest_falls_back_to_stats_when_llm_off(self):
        self.plugin.config["llm_mode"] = "off"
        for i in range(3):
            await self.plugin._on_events(
                [DeviceEvent(type="print_started" if i % 2 else "print_finished", data={})], None
            )
        text = await self.run_cmd("digest")
        self.assertIn("纯统计", text)
        self.assertFalse(self.context.llm_calls)

    async def test_device_list_marks_dryer_unimplemented(self):
        text = await self.run_cmd("device", "/banbu device list")
        self.assertIn("bambu_a1", text)
        self.assertIn("dryer_esp32", text)
        self.assertIn("二期", text)
        self.assertIn("未启用", text)

    async def test_subscribe_and_unsubscribe(self):
        text = await self.run_cmd("subscribe", "/banbu subscribe")
        self.assertIn("已加入", text)
        self.assertIn("stub:FriendMessage:1001", self.plugin.subscribers.umos())
        text = await self.run_cmd("subscribe", "/banbu subscribe")
        self.assertIn("本来就在", text)
        text = await self.run_cmd("unsubscribe", "/banbu unsubscribe")
        self.assertIn("已退出", text)
        self.assertEqual(self.plugin.subscribers.umos(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
