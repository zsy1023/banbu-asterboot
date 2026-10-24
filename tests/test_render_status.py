"""状态卡视觉细节：小打印机像素图、温度小进度条。

这两块都是纯排版，不依赖客户端支持富文本；测试保证它们：
  1. 每行等宽（否则在等宽字体里会歪掉）；
  2. 只用方块字符/半角空格，不含 CJK 与 emoji；
  3. 温度条只在"有目标温度"时出现，且填充比例正确。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bambu_a1 import render  # noqa: E402
from bambu_a1.compat import load_profile  # noqa: E402

BLOCKS = set("▄▀█▰▱░▒▓ ")


def display_width(text: str) -> int:
    """粗略宽度：CJK 记 2，其余记 1。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


class TestPrinterArt(unittest.TestCase):
    def test_art_is_four_lines_and_rectangular(self):
        art = render.PRINTER_ART
        self.assertGreaterEqual(len(art), 3, "小打印机至少 3 行")
        self.assertLessEqual(len(art), 4, "小打印机最多 4 行")
        widths = {display_width(line) for line in art}
        self.assertEqual(len(widths), 1, f"各行宽度不一致：{widths} → {art}")

    def test_art_uses_only_block_characters(self):
        for line in render.PRINTER_ART:
            leftover = set(line) - BLOCKS
            self.assertFalse(leftover, f"像素图里出现了非方块字符：{leftover}")

    def test_status_card_contains_art_after_header(self):
        profile = load_profile("a1_v1")
        snap = profile.to_snapshot({"print": {
            "gcode_state": "RUNNING", "mc_percent": 25,
            "nozzle_temper": 215, "nozzle_target_temper": 215,
        }})
        lines = render.render_status(snap).splitlines()
        self.assertEqual(lines[0], "拓竹 A1 · 打印中")
        self.assertEqual(lines[1], render.RULE)
        self.assertEqual(tuple(lines[2 : 2 + len(render.PRINTER_ART)]), render.PRINTER_ART)
        after_art = 2 + len(render.PRINTER_ART)
        self.assertEqual(lines[after_art], "", "像素图后应空一行再放细节")
        self.assertIn("进度", lines[after_art + 1])

    def test_no_art_when_no_data(self):
        profile = load_profile("a1_v1")
        text = render.render_status(profile.to_snapshot({}))
        self.assertIn("暂无数据", text)
        self.assertNotIn(render.PRINTER_ART[1], text)


class TestTempBar(unittest.TestCase):
    def test_at_target_is_full(self):
        self.assertEqual(render.temp_bar(215, 215), "▰" * 8 + " 215/215°C")

    def test_heating_is_partial(self):
        bar = render.temp_bar(120, 215)
        self.assertTrue(bar.startswith("▰"), bar)
        self.assertIn("▱", bar)
        self.assertTrue(bar.endswith("120/215°C"), bar)
        filled = bar.count("▰")
        self.assertTrue(2 <= filled <= 6, f"120/215 应填充一半左右，实际 {filled}")

    def test_over_target_is_clamped(self):
        self.assertEqual(render.temp_bar(230, 215), "▰" * 8 + " 230/215°C")

    def test_no_target_means_no_bar(self):
        self.assertEqual(render.temp_bar(28, 0), "28°C")
        self.assertEqual(render.temp_bar(28, None), "28°C")

    def test_missing_reading_returns_none(self):
        self.assertIsNone(render.temp_bar(None, 200))

    def test_status_card_shows_two_bars(self):
        profile = load_profile("a1_v1")
        snap = profile.to_snapshot({"print": {
            "gcode_state": "RUNNING", "nozzle_temper": 215, "nozzle_target_temper": 215,
            "bed_temper": 30, "bed_target_temper": 60, "chamber_temper": 28,
        }})
        text = render.render_status(snap)
        self.assertIn("喷头　" + "▰" * 8 + " 215/215°C", text)
        self.assertIn("热床　" + "▰" * 4 + "▱" * 4 + " 30/60°C", text)
        self.assertIn("腔体　28°C", text, "腔体没有目标温度，只给数值")
        self.assertNotIn("腔体　▰", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
