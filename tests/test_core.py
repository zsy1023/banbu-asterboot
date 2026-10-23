"""核心逻辑单测：不联网、不依赖 astrbot。

跑法（在插件目录下）：
    python tests/run_tests.py
或：
    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bambu_a1 import render, router  # noqa: E402
from bambu_a1.coalescer import Coalescer  # noqa: E402
from bambu_a1.compat import Profile, load_profile  # noqa: E402
from bambu_a1.credentials import (  # noqa: E402
    CredentialBox,
    DecryptError,
    mask_phone,
    mask_serial,
)
from bambu_a1.events import EventState, detect  # noqa: E402
from bambu_a1.models import (  # noqa: E402
    CH_BOTH,
    CH_DIRECT,
    CH_LLM,
    EV_HMS_ERROR,
    EV_MILESTONE,
    EV_PRINT_FINISHED,
    EV_PRINT_PAUSED,
    EV_PRINT_STARTED,
    DeviceEvent,
    Snapshot,
    deep_merge,
    dig,
)
from bambu_a1.push import in_quiet_hours, parse_quiet_range  # noqa: E402
from bambu_a1.rules import RuleEngine, parse_temp_range  # noqa: E402
from bambu_a1.store import append_jsonl, read_json, tail_jsonl, write_json  # noqa: E402


# ─────────────────────────── 夹具 ───────────────────────────
def raw_print(**overrides) -> dict:
    payload = {
        "gcode_state": "RUNNING",
        "mc_percent": 42,
        "layer_num": 100,
        "total_layer_num": 240,
        "mc_remaining_time": 65,
        "mc_print_time": 30,
        "mc_print_total_time": 95,
        "nozzle_temper": 215.0,
        "nozzle_target_temper": 215.0,
        "bed_temper": 60.0,
        "bed_target_temper": 60.0,
        "chamber_temper": 32.0,
        "subtask_name": "benchy_0.2mm",
        "task_id": "task-1",
        "spd_mag": 100,
        "wifi_signal": "-52dBm",
        "stg_cur": 2,
        "lights_report": [{"mode": "on"}],
        "ams": {"ams": [{"id": "0", "humidity": "3", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_remain": 78},
        ]}]},
        "vtray": {"tray": [{"tray_type": "PETG", "tray_color": "000000FF", "tray_remain": 45}]},
        "hms": [],
        "cooling_fan_speed": "14",
    }
    payload.update(overrides)
    return {"print": payload}


def snap_from(profile: Profile, **overrides) -> Snapshot:
    return profile.to_snapshot(raw_print(**overrides))


# ─────────────────────────── 模型 ───────────────────────────
class TestModels(unittest.TestCase):
    def test_deep_merge_keeps_untouched_keys(self):
        dst = {"print": {"a": 1, "b": {"c": 2}}}
        deep_merge(dst, {"print": {"b": {"c": 9}, "d": 4}})
        self.assertEqual(dst["print"], {"a": 1, "b": {"c": 9}, "d": 4})

    def test_dig_list_index_and_missing(self):
        data = {"print": {"lights_report": [{"mode": "on"}]}}
        self.assertEqual(dig(data, "print.lights_report.0.mode"), "on")
        self.assertIsNone(dig(data, "print.nope.deep"))

    def test_snapshot_roundtrip_drops_raw(self):
        snap = Snapshot(percent=10, task_name="x", raw={"print": {}})
        data = snap.to_dict()
        self.assertNotIn("raw", data)
        self.assertEqual(Snapshot.from_dict(data).task_name, "x")


# ─────────────────────────── 协议档案 ───────────────────────────
class TestCompat(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def test_field_mapping_and_casts(self):
        snap = snap_from(self.profile)
        self.assertEqual(snap.state, "printing")
        self.assertEqual(snap.percent, 42)
        self.assertEqual(snap.layer, 100)
        self.assertEqual(snap.nozzle, 215.0)
        self.assertEqual(snap.light, "on")
        self.assertEqual(snap.fans["cooling"], 100)  # 14 档 → 100%
        self.assertEqual(len(snap.ams), 1)
        self.assertEqual(snap.ams[0]["trays"][0]["type"], "PLA")
        self.assertEqual(snap.vtray["type"], "PETG")

    def test_endpoints_per_region(self):
        cn = self.profile.endpoints("China")
        us = self.profile.endpoints("Global")
        self.assertIn("api.bambulab.cn", cn["login_url"])
        self.assertIn("api.bambulab.com", us["login_url"])
        self.assertEqual(cn["sms_url"], us["sms_url"])  # 收码域名两区共用

    def test_state_normalization_and_unknown_tracking(self):
        self.assertEqual(self.profile.normalize_state("PAUSE"), "paused")
        self.assertEqual(self.profile.normalize_state("WEIRD_STATE"), "weird_state")
        self.assertIn("WEIRD_STATE", self.profile.unknown_states)

    def test_missing_field_is_none_not_zero(self):
        profile = load_profile("a1_v1")
        snap = profile.to_snapshot({"print": {"gcode_state": "IDLE"}})
        self.assertIsNone(snap.percent)
        self.assertIn("percent", profile.missing_fields)

    def test_pause_reason_priority(self):
        profile = load_profile("a1_v1")
        reason, key, detail = profile.pause_reason({"print": {"gcode_state": "PAUSE", "stg_cur": 16}})
        self.assertEqual(key, "manual_pause")
        self.assertTrue(reason)
        self.assertEqual(detail["stage"], 16)

    def test_drift_detects_new_field(self):
        profile = load_profile("a1_v1")
        profile.to_snapshot({"print": {"gcode_state": "IDLE", "brand_new_field": 1}})
        self.assertIn("print.brand_new_field", profile.drift_report()["unknown_fields"])

    def test_overrides_patch_field_map(self):
        profile = load_profile("a1_v1", {"fields": {"percent": {"paths": ["print.new_percent"], "cast": "int"}}})
        snap = profile.to_snapshot({"print": {"gcode_state": "RUNNING", "new_percent": 88}})
        self.assertEqual(snap.percent, 88)


# ─────────────────────────── 事件检测 ───────────────────────────
class TestEventDetect(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def _detect(self, prev, cur, milestones=None, first_poll=False):
        return detect(prev, cur, self.profile, milestones=milestones, first_poll=first_poll)

    def test_print_started(self):
        prev = snap_from(self.profile, gcode_state="IDLE")
        cur = snap_from(self.profile, gcode_state="RUNNING")
        types = [e.type for e in self._detect(prev, cur)]
        self.assertIn(EV_PRINT_STARTED, types)

    def test_paused_with_reason(self):
        prev = snap_from(self.profile, gcode_state="RUNNING")
        cur = snap_from(self.profile, gcode_state="PAUSE", stg_cur=16)
        events = self._detect(prev, cur)
        paused = [e for e in events if e.type == EV_PRINT_PAUSED]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0].data["reason_key"], "manual_pause")

    def test_filament_runout_emits_extra_event(self):
        profile = load_profile("a1_v1", {"stage_pause_reasons": {"7": "filament_runout"}})
        prev = snap_from(profile, gcode_state="RUNNING")
        cur = snap_from(profile, gcode_state="PAUSE", stg_cur=7)
        types = [e.type for e in detect(prev, cur, profile)]
        self.assertIn("filament_runout", types)

    def test_finished_and_first_poll_suppression(self):
        prev = snap_from(self.profile, gcode_state="RUNNING")
        cur = snap_from(self.profile, gcode_state="FINISH", mc_percent=100)
        self.assertIn(EV_PRINT_FINISHED, [e.type for e in self._detect(prev, cur)])

        # 启动首轮：当前已 FINISH，不应误报
        types = [e.type for e in self._detect(None, cur, first_poll=True)]
        self.assertNotIn(EV_PRINT_FINISHED, types)

    def test_hms_new_codes(self):
        prev = snap_from(self.profile, hms=[])
        cur = snap_from(self.profile, hms=[{"code": "03004002"}])
        events = [e for e in self._detect(prev, cur) if e.type == EV_HMS_ERROR]
        self.assertEqual(events[0].data["codes"], ["03004002"])
        self.assertEqual(events[0].severity, "critical")

    def test_hms_not_reported_on_first_poll(self):
        """绑定时机器上已有故障码，不应被当成"刚发生"而报一次故障。"""
        cur = snap_from(self.profile, hms=[{"code": "03004002"}])
        self.assertEqual(
            [e for e in self._detect(None, cur, first_poll=True) if e.type == EV_HMS_ERROR], []
        )
        # 没传 first_poll 但也没有 prev 时同样不报
        self.assertEqual([e for e in self._detect(None, cur) if e.type == EV_HMS_ERROR], [])

    def test_milestone_crossing(self):
        prev = snap_from(self.profile, mc_percent=40)
        cur = snap_from(self.profile, mc_percent=52)
        events = [e for e in self._detect(prev, cur, milestones=[25, 50, 75]) if e.type == EV_MILESTONE]
        self.assertEqual([e.data["percent"] for e in events], [50])

    def test_no_milestones_on_first_poll(self):
        """绑定后的首轮只是建基线：机器正好停在 100% 时不能喷出 25/50/75/90。"""
        cur = snap_from(self.profile, mc_percent=100)
        events = self._detect(None, cur, milestones=[25, 50, 75, 90], first_poll=True)
        self.assertEqual([e for e in events if e.type == EV_MILESTONE], [])

        # 即使调用方忘了传 first_poll，只要没有 prev 也不该报
        events = self._detect(None, cur, milestones=[25, 50, 75, 90])
        self.assertEqual([e for e in events if e.type == EV_MILESTONE], [])

    def test_no_milestones_when_not_printing(self):
        """已 FINISH / PAUSE / IDLE 时跨过阈值没有意义（截图里那 4 条假进度）。"""
        prev = snap_from(self.profile, gcode_state="FINISH", mc_percent=90)
        cur = snap_from(self.profile, gcode_state="FINISH", mc_percent=100)
        self.assertEqual(
            [e for e in self._detect(prev, cur, milestones=[25, 50, 75, 90]) if e.type == EV_MILESTONE],
            [],
        )
        # 暂停中也不报
        prev = snap_from(self.profile, gcode_state="PAUSE", mc_percent=40)
        cur = snap_from(self.profile, gcode_state="PAUSE", mc_percent=60)
        self.assertEqual(
            [e for e in self._detect(prev, cur, milestones=[50]) if e.type == EV_MILESTONE],
            [],
        )

    def test_milestone_needs_known_previous_percent(self):
        """prev 的进度未知时无法判断"刚跨过"，宁可不报。"""
        prev = self.profile.to_snapshot({"print": {"gcode_state": "RUNNING", "task_id": "t1"}})
        cur = snap_from(self.profile, mc_percent=60)
        self.assertIsNone(prev.percent)
        self.assertEqual(
            [e for e in self._detect(prev, cur, milestones=[50]) if e.type == EV_MILESTONE],
            [],
        )

    def test_no_events_when_state_unchanged(self):
        prev = snap_from(self.profile)
        cur = snap_from(self.profile)
        self.assertEqual(self._detect(prev, cur, milestones=[25, 50]), [])


class TestEventStateDedup(unittest.TestCase):
    def test_dedup_and_task_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = EventState(os.path.join(tmp, "state.json"))
            evt = DeviceEvent(type="print_finished", dedup_key="k1")
            self.assertEqual(len(state.filter_new([evt])), 1)
            self.assertEqual(len(state.filter_new([evt])), 0)  # 重复被吃掉

            snap = snap_from(load_profile("a1_v1"))
            self.assertTrue(state.sync_task(snap))            # 新任务 → 清空
            self.assertEqual(len(state.filter_new([evt])), 1)

    def test_persisted_across_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = EventState(path)
            snap = snap_from(load_profile("a1_v1"))
            state.save(snap)
            reloaded = EventState(path)
            self.assertIsNotNone(reloaded.last_snapshot)
            self.assertEqual(reloaded.last_snapshot.percent, 42)


# ─────────────────────────── 规则引擎 ───────────────────────────
class TestRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = RuleEngine(os.path.join(self.tmp.name, "rules.json"))
        self.profile = load_profile("a1_v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _hit(self, rtype, value, value2=None, prev_kw=None, cur_kw=None):
        rule, error = self.engine.add(rtype, value, value2)
        self.assertIsNotNone(rule, error)
        prev = snap_from(self.profile, **(prev_kw or {}))
        cur = snap_from(self.profile, **(cur_kw or {}))
        events = self.engine.evaluate(prev, cur)
        return [e for e in events if e.rule_id == rule.id]

    def test_layer_reached(self):
        self.assertTrue(self._hit("layer_reached", 150, prev_kw={"layer_num": 100},
                                  cur_kw={"layer_num": 160}))
        self.assertFalse(self._hit("layer_reached", 200, prev_kw={"layer_num": 100},
                                   cur_kw={"layer_num": 160}))

    def test_percent_reached(self):
        self.assertTrue(self._hit("percent_reached", 50, prev_kw={"mc_percent": 40},
                                  cur_kw={"mc_percent": 55}))

    def test_print_remaining(self):
        self.assertTrue(self._hit("print_remaining", 30, prev_kw={"mc_remaining_time": 40},
                                   cur_kw={"mc_remaining_time": 25}))

    def test_print_duration(self):
        self.assertTrue(self._hit("print_duration", 60, prev_kw={"mc_print_time": 50},
                                   cur_kw={"mc_print_time": 61}))

    def test_nozzle_and_bed_temp_above(self):
        self.assertTrue(self._hit("nozzle_temp_above", 210,
                                  prev_kw={"nozzle_temper": 205.0}, cur_kw={"nozzle_temper": 230.0}))
        self.assertTrue(self._hit("bed_temp_above", 55,
                                  prev_kw={"bed_temper": 50.0}, cur_kw={"bed_temper": 65.0}))

    def test_temp_below(self):
        self.assertTrue(self._hit("nozzle_temp_below", 100,
                                  prev_kw={"nozzle_temper": 120.0}, cur_kw={"nozzle_temper": 90.0}))

    def test_chamber_temp_above(self):
        self.assertTrue(self._hit("chamber_temp_above", 40,
                                  prev_kw={"chamber_temper": 35.0}, cur_kw={"chamber_temper": 45.0}))

    def test_temp_in_range(self):
        self.assertTrue(self._hit("temp_in_range", 30, 40,
                                  prev_kw={"bed_temper": 20.0}, cur_kw={"bed_temper": 35.0}))
        self.assertFalse(self._hit("temp_in_range", 30, 40,
                                   prev_kw={"bed_temper": 35.0}, cur_kw={"bed_temper": 36.0}))

    def test_hms_code(self):
        self.assertTrue(self._hit("hms_code", "0300",
                                  prev_kw={"hms": []}, cur_kw={"hms": [{"code": "03004002"}]}))

    def test_filament_low(self):
        payload = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_remain": 5}]}]}}
        self.assertTrue(self._hit("filament_low", 10, cur_kw=payload))

    def test_validate_rejects_bad_input(self):
        rule, error = self.engine.add("layer_reached", "abc")
        self.assertIsNone(rule)
        self.assertIn("数字", error)
        rule, error = self.engine.add("percent_reached", 150)
        self.assertIsNone(rule)
        rule, error = self.engine.add("no_such_type", 1)
        self.assertIsNone(rule)

    def test_add_delete_persist(self):
        rule, _ = self.engine.add("layer_reached", 50, label="半程提醒")
        fresh = RuleEngine(self.engine.path)
        self.assertEqual(len(fresh.all()), 1)
        self.assertTrue(fresh.delete(rule.id))
        self.assertEqual(len(RuleEngine(self.engine.path).all()), 0)

    def test_parse_temp_range(self):
        self.assertEqual(parse_temp_range("30-40"), (30.0, 40.0))
        self.assertEqual(parse_temp_range("40~30"), (30.0, 40.0))
        self.assertIsNone(parse_temp_range("abc"))

    def test_offline_rule_shortens_watchdog(self):
        from bambu_a1.monitor import MonitorEngine

        self.assertIsNone(self.engine.offline_threshold_minutes())
        self.engine.add("offline", 2)
        self.engine.add("offline", 5)
        self.assertEqual(self.engine.offline_threshold_minutes(), 2.0)

        monitor = MonitorEngine(
            adapter=object(),  # 这个判定不碰适配器
            profile=self.profile,
            rules=self.engine,
            state=None,
            config=lambda key, default=None: {"stale_after": 600}.get(key, default),
        )
        self.assertEqual(monitor._offline_threshold_s(), 120, "应取用户规则的 2 分钟")


# ─────────────────────────── 路由 ───────────────────────────
class TestRouter(unittest.TestCase):
    def test_default_routes(self):
        # 出厂分界线按 full 档看（默认档 summary_only 只会放行完成复盘）
        self.assertEqual(router.resolve(DeviceEvent(type=EV_PRINT_PAUSED), llm_mode="full"), CH_DIRECT)
        self.assertEqual(router.resolve(DeviceEvent(type=EV_PRINT_FINISHED), llm_mode="full"), CH_LLM)
        self.assertEqual(router.resolve(DeviceEvent(type=EV_HMS_ERROR), llm_mode="full"), CH_LLM)

    def test_mode_gate(self):
        finished = DeviceEvent(type=EV_PRINT_FINISHED)
        self.assertEqual(router.resolve(finished, llm_mode="off"), CH_DIRECT)
        self.assertEqual(router.resolve(finished, llm_mode="summary_only"), CH_LLM)
        # summary_only 下诊断类不走模型
        failed = DeviceEvent(type="print_failed")
        self.assertEqual(router.resolve(failed, llm_mode="summary_only"), CH_DIRECT)
        self.assertEqual(router.resolve(failed, llm_mode="full"), CH_LLM)

    def test_rule_channel_wins(self):
        evt = DeviceEvent(type=EV_PRINT_PAUSED, data={"channel": "llm"})
        self.assertEqual(router.resolve(evt, llm_mode="full"), CH_LLM)

    def test_config_override(self):
        evt = DeviceEvent(type=EV_PRINT_PAUSED)
        self.assertEqual(
            router.resolve(evt, llm_route={"print_paused": "both"}, llm_mode="full"), CH_BOTH
        )

    def test_unknown_event_defaults_direct(self):
        self.assertEqual(router.resolve(DeviceEvent(type="brand_new_event")), CH_DIRECT)

    def test_describe_marks_downgrade(self):
        rows = dict((t, (c, e)) for t, c, e in router.describe({}, "off"))
        self.assertEqual(rows["print_finished"], ("llm", "direct"))


# ─────────────────────────── 合并窗口 ───────────────────────────
class TestCoalescer(unittest.IsolatedAsyncioTestCase):
    async def test_merges_into_one_batch(self):
        batches = []

        async def sink(events):
            batches.append(events)

        coal = Coalescer(sink, window_s=0.05, critical_window_s=0.05)
        await coal.submit([
            DeviceEvent(type=EV_PRINT_STARTED, data={}),
            DeviceEvent(type=EV_MILESTONE, data={"percent": 50}),
        ])
        await asyncio.sleep(0.2)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 2)

    async def test_llm_draft_merged_when_fast(self):
        batches = []

        async def sink(events):
            batches.append(events)

        async def draft(event):
            return "模型补写的一段解释"

        coal = Coalescer(sink, window_s=0.2, critical_window_s=0.2, draft=draft)
        await coal.submit([DeviceEvent(type=EV_PRINT_FINISHED, channel=CH_LLM, data={})])
        await asyncio.sleep(0.4)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0].data.get("llm_text"), "模型补写的一段解释")

    async def test_slow_draft_dropped_but_fact_still_sent(self):
        batches = []

        async def sink(events):
            batches.append(events)

        async def slow_draft(event):
            await asyncio.sleep(5)
            return "永远到不了"

        coal = Coalescer(sink, window_s=0.05, critical_window_s=0.05, draft=slow_draft)
        await coal.submit([DeviceEvent(type=EV_PRINT_FINISHED, channel=CH_LLM, data={})])
        await asyncio.sleep(0.3)
        self.assertEqual(len(batches), 1, "模型超窗时事实必须照发")
        self.assertNotIn("llm_text", batches[0][0].data)

    async def test_critical_uses_shorter_window(self):
        batches = []

        async def sink(events):
            batches.append(events)

        coal = Coalescer(sink, window_s=5, critical_window_s=0.05)
        await coal.submit([DeviceEvent(type=EV_PRINT_PAUSED, severity="critical")])
        await asyncio.sleep(0.2)
        self.assertEqual(len(batches), 1)

    async def test_later_events_do_not_postpone_window(self):
        """窗口从第一条事件开始计时，后续事件不能把截止时间一直往后推。"""
        batches = []

        async def sink(events):
            batches.append(events)

        coal = Coalescer(sink, window_s=0.2, critical_window_s=0.2)
        await coal.submit([DeviceEvent(type=EV_PRINT_STARTED, data={})])
        await asyncio.sleep(0.12)
        await coal.submit([DeviceEvent(type=EV_MILESTONE, data={"percent": 25})])
        await asyncio.sleep(0.15)  # 距第一条已经 0.27s > 0.2s
        self.assertEqual(len(batches), 1, "窗口被推迟了")
        self.assertEqual(len(batches[0]), 2, "两条事件应合并进同一条")
        self.assertGreaterEqual(batches[0][0].ts, 0)


# ─────────────────────────── 渲染 ───────────────────────────
class TestRender(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def test_status_card_has_no_none(self):
        snap = snap_from(self.profile)
        text = render.render_status(snap, state_age_s=12)
        self.assertIn("打印中", text)
        self.assertIn("42%", text)
        self.assertNotIn("None", text)

    def test_missing_values_are_omitted(self):
        snap = self.profile.to_snapshot({"print": {"gcode_state": "IDLE"}})
        text = render.render_status(snap)
        self.assertNotIn("None", text)
        self.assertIn("空闲", text)

    def test_event_rendering(self):
        evt = DeviceEvent(
            type=EV_PRINT_PAUSED,
            severity="critical",
            data={"reason": "耗材用尽（断料）", "percent": 47, "layer": 112},
        )
        text = render.render_event(evt)
        self.assertIn("打印暂停", text)
        self.assertIn("耗材用尽", text)
        self.assertIn("/banbu resume", text)

    def test_batch_appends_llm_text(self):
        events = [
            DeviceEvent(type=EV_PRINT_FINISHED, data={"task_name": "benchy", "elapsed_min": 102,
                                                     "llm_text": "这次层纹比上次好。"}),
        ]
        text = render.render_batch(events)
        self.assertIn("打印完成", text)
        self.assertIn("这次层纹比上次好。", text)
        self.assertIn("———", text)

    def test_fmt_minutes(self):
        self.assertEqual(render.fmt_minutes(102), "1h 42m")
        self.assertEqual(render.fmt_minutes(45), "45 分钟")
        self.assertEqual(render.fmt_minutes(None), "未知")


# ─────────────────────────── 监控引擎集成 ───────────────────────────
class TestMonitorIntegration(unittest.IsolatedAsyncioTestCase):
    """用脚本化快照驱动 MonitorEngine，验证 IDLE→RUNNING→PAUSE→FINISH 全链路。"""

    class _FakePoll:
        def __init__(self):
            self.queue: list[dict] = []

        async def fetch(self):
            return self.queue.pop(0) if self.queue else None

    class _FakeAdapter:
        key = "fake"

        def __init__(self):
            self._poll = TestMonitorIntegration._FakePoll()

        def poll_source(self):
            return self._poll

        def push_source(self):
            return None

        def stop_stream(self):
            return None

        def to_snapshot(self, raw):
            return self.profile.to_snapshot(raw)  # type: ignore[attr-defined]

    async def asyncSetUp(self):
        from bambu_a1.monitor import MonitorEngine

        self.tmp = tempfile.TemporaryDirectory()
        self.profile = load_profile("a1_v1")
        self.adapter = self._FakeAdapter()
        self.adapter.profile = self.profile
        self.rules = RuleEngine(os.path.join(self.tmp.name, "rules.json"))
        self.state = EventState(os.path.join(self.tmp.name, "state.json"))
        self.events: list[DeviceEvent] = []
        self.snapshots: list[Snapshot] = []
        self.monitor = MonitorEngine(
            adapter=self.adapter,
            profile=self.profile,
            rules=self.rules,
            state=self.state,
            config=lambda key, default=None: {"stale_after": 90, "progress_milestones": [50]}.get(key, default),
            on_snapshot=self._on_snapshot,
            on_events=self._on_events,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _on_snapshot(self, snap):
        self.snapshots.append(snap)

    async def _on_events(self, events, snap):
        self.events.extend(events)

    async def _push(self, **overrides):
        await self.monitor._handle_raw(raw_print(**overrides))

    def _types(self) -> list[str]:
        return [e.type for e in self.events]

    async def test_full_print_lifecycle(self):
        await self._push(gcode_state="IDLE", mc_percent=0, layer_num=0)
        self.assertNotIn(EV_PRINT_STARTED, self._types(), "首轮只建基线，不误报")

        await self._push(gcode_state="RUNNING", mc_percent=5, layer_num=10)
        self.assertIn(EV_PRINT_STARTED, self._types())

        # 里程碑只在"正在打印"且跨过阈值时触发
        await self._push(gcode_state="RUNNING", mc_percent=52, layer_num=120)
        self.assertIn(EV_MILESTONE, self._types(), "打印中跨过 50% 应有里程碑")

        await self._push(gcode_state="PAUSE", mc_percent=52, layer_num=120, stg_cur=16)
        self.assertIn(EV_PRINT_PAUSED, self._types())

        await self._push(gcode_state="RUNNING", mc_percent=52, layer_num=121)
        self.assertIn("print_resumed", self._types())

        await self._push(gcode_state="FINISH", mc_percent=100, layer_num=240)
        self.assertIn(EV_PRINT_FINISHED, self._types())
        # FINISH 时剩余时间归零，渲染层不应出现"剩 0 分钟"
        milestone_texts = [
            render.render_event(e) for e in self.events if e.type == EV_MILESTONE
        ]
        self.assertTrue(milestone_texts)
        self.assertNotIn("剩余：0 分钟", " ".join(milestone_texts))

        # 同一状态重复上报不应重复触发
        before = len(self.events)
        await self._push(gcode_state="FINISH", mc_percent=100, layer_num=240)
        self.assertEqual(len(self.events), before)

    async def test_binding_while_finished_emits_nothing(self):
        """截图里的事故复现：机器停在 100% 时绑定，首轮不能喷出 4 条假进度。"""
        await self._push(gcode_state="FINISH", mc_percent=100, layer_num=240)
        self.assertEqual(self.events, [], "首轮基线不应产生任何事件")

    async def test_offline_detected_after_silence(self):
        await self._push(gcode_state="RUNNING")
        self.events.clear()
        # 把最近一份快照的时间推回 10 分钟前，模拟没有新报文
        self.state.last_snapshot.ts -= 600
        await self.monitor._check_stale()
        self.assertIn("offline", self._types())

        # 恢复后应有 online
        await self._push(gcode_state="RUNNING")
        self.assertIn("online", self._types())

    async def test_user_offline_rule_shortens_threshold(self):
        base = self.monitor._offline_threshold_s()
        self.assertEqual(base, 90)
        self.rules.add("offline", 1)  # 1 分钟 = 60s < 90s，应该缩短
        self.assertEqual(self.monitor._offline_threshold_s(), 60)
        self.rules.clear()
        self.rules.add("offline", 10)  # 600s > 90s，仍以 stale_after 为准
        self.assertEqual(self.monitor._offline_threshold_s(), 90)

    async def test_snapshot_callback_receives_normalized_data(self):
        await self._push(gcode_state="RUNNING", mc_percent=33)
        snap = self.snapshots[-1]
        self.assertEqual(snap.state, "printing")
        self.assertEqual(snap.percent, 33)
        self.assertEqual(snap.task_name, "benchy_0.2mm")


# ─────────────────────────── 推送与安静时段 ───────────────────────────
class TestQuietHours(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_quiet_range("23:00-07:30"), (1380, 450))
        self.assertIsNone(parse_quiet_range("bad"))

    def test_wraparound(self):
        self.assertTrue(in_quiet_hours("23:00-07:30", 23 * 60 + 30))
        self.assertTrue(in_quiet_hours("23:00-07:30", 2 * 60))
        self.assertFalse(in_quiet_hours("23:00-07:30", 12 * 60))

    def test_normal_range(self):
        self.assertTrue(in_quiet_hours("01:00-05:00", 2 * 60))
        self.assertFalse(in_quiet_hours("01:00-05:00", 6 * 60))


# ─────────────────────────── 存储与凭据 ───────────────────────────
class TestStore(unittest.TestCase):
    def test_json_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            write_json(path, {"k": 1})
            self.assertEqual(read_json(path, {}), {"k": 1})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{broken")
            self.assertEqual(read_json(path, {"fallback": True}), {"fallback": True})
            self.assertTrue(os.path.exists(path + ".bak"))

    def test_jsonl_append_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "e.jsonl")
            for i in range(5):
                append_jsonl(path, {"i": i})
            self.assertEqual([r["i"] for r in tail_jsonl(path, 2)], [3, 4])

    def test_jsonl_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "e.jsonl")
            for i in range(3):
                append_jsonl(path, {"i": i}, max_bytes=10)
            self.assertTrue(os.path.exists(path + ".1"))


class TestCredentials(unittest.TestCase):
    def test_roundtrip(self):
        box = CredentialBox("master-secret", instance_id="inst-1")
        blob = box.encrypt("access-token-abc")
        self.assertNotIn("access-token-abc", str(blob))
        self.assertEqual(box.decrypt(blob), "access-token-abc")

    def test_wrong_secret_is_explicit_error(self):
        box1 = CredentialBox("secret-a", instance_id="inst-1")
        box2 = CredentialBox("secret-b", instance_id="inst-1")
        blob = box1.encrypt("token")
        with self.assertRaises(DecryptError) as ctx:
            box2.decrypt(blob)
        self.assertIn("重新", str(ctx.exception))

    def test_different_machine_salt_breaks_decrypt(self):
        box1 = CredentialBox("same-secret", instance_id="inst-1")
        box2 = CredentialBox("same-secret", instance_id="inst-2")
        blob = box1.encrypt("token")
        with self.assertRaises(DecryptError):
            box2.decrypt(blob)

    def test_masking(self):
        self.assertEqual(mask_phone("13800001111"), "138****1111")
        self.assertNotIn("1234", mask_serial("01P00A1234"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
