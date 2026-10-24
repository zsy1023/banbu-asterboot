"""插件自身元数据解析（版本/仓库）与 `/banbu version` 的一致性测试。

更新检测本身是 AstrBot 的内置能力（WebUI 插件页 → 更新），插件侧不轮询远端。
这里只保证：插件能正确读出自己的版本，且 metadata 里的 repo 与真实远端一致
——这两点是"框架能识别更新"的前提。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bambu_a1 import about  # noqa: E402

SAMPLE = """\
name: astrbot_plugin_demo
display_name: 演示插件
short_desc: 一句话
desc: |
  多行描述
  version: 9.9.9
version: 1.2.3
author: someone
repo: https://github.com/someone/demo
astrbot_version: ">=4.9.2"
tags:
  - demo
"""


class TestMetadataParsing(unittest.TestCase):
    def test_reads_top_level_scalars(self):
        meta = about.parse_metadata_text(SAMPLE)
        self.assertEqual(meta["name"], "astrbot_plugin_demo")
        self.assertEqual(meta["version"], "1.2.3")
        self.assertEqual(meta["repo"], "https://github.com/someone/demo")
        self.assertEqual(meta["author"], "someone")

    def test_ignores_multiline_block_and_lists(self):
        meta = about.parse_metadata_text(SAMPLE)
        self.assertNotEqual(meta.get("desc"), "|", "desc 是多行块，不该被当成值")
        self.assertNotIn("9.9.9", meta.values(), "多行块里的伪字段不能被读到")
        self.assertNotIn("demo", meta.values(), "列表项不能被当成字段")

    def test_quoted_values_are_unwrapped(self):
        meta = about.parse_metadata_text('astrbot_version: ">=4.9.2"\n')
        self.assertEqual(meta["astrbot_version"], ">=4.9.2")

    def test_missing_file_returns_empty(self):
        self.assertEqual(about.read_metadata(os.path.join(_ROOT, "no-such-dir")), {})

    def test_reads_real_plugin_metadata(self):
        meta = about.read_metadata(_ROOT)
        self.assertTrue(meta.get("name", "").startswith("astrbot_plugin_"))
        self.assertRegex(meta.get("version", ""), r"^\d+\.\d+\.\d+$")
        self.assertIn("github.com", meta.get("repo", ""))

    def test_plugin_version_matches_changelog_and_git_tag_style(self):
        meta = about.read_metadata(_ROOT)
        version = meta["version"]
        changelog = open(os.path.join(_ROOT, "CHANGELOG.md"), encoding="utf-8").read()
        self.assertIn(f"## {version}", changelog, "CHANGELOG 必须有当前版本条目")
        self.assertFalse(version.startswith("v"), "metadata.version 不能带 v 前缀")


class TestVersionCommand(unittest.TestCase):
    def test_version_text_mentions_repo_and_update_hint(self):
        """`/banbu version` 的输出必须能自证版本并指路更新入口。"""
        meta = about.read_metadata(_ROOT)
        # 这里只校验文案素材存在；命令本身的装配由冒烟测试覆盖
        self.assertIn("repo", meta)
        helper_source = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn('@banbu.command("version")', helper_source)
        self.assertIn("更换安装源", helper_source, "应提示没有安装源时怎么绑定")

    def test_main_has_no_emoji_in_replies(self):
        """命令回执统一走纯文字排版（与 render.py 的零 emoji 约定一致）。"""
        ranges = [(0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0xFE0F, 0xFE0F)]
        text = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        offenders = [
            (line_no, ch)
            for line_no, line in enumerate(text.splitlines(), 1)
            for ch in line
            if any(low <= ord(ch) <= high for low, high in ranges)
        ]
        self.assertFalse(offenders, f"main.py 里还有 emoji：{offenders[:5]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
