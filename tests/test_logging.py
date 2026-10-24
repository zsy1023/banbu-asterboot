"""日志规范合规测试。

上架审核要求：**logger 必须且只能来自 `astrbot.api`**（`from astrbot.api import logger`），
严禁在插件里使用 Python 内置 `logging` 自建 logger。

历史教训（0.1.2 线上事故）：自己 `logging.getLogger("astrbot.plugin.bambu_a1.*")` 建的 logger
不是 `LogManager.get_plugin_logger()` 创建的，记录冒泡到宿主 `astrbot` logger 上那个
格式串含 `%(plugin_tag)s` 的 `LogQueueHandler` 时，`self.format(record)` 会抛
`ValueError: Formatting field not found in record: 'plugin_tag'`，一条 INFO 日志就能把命令打挂。

所以这里用 AST 扫描把规则钉死：
  - 插件代码里不得出现 `import logging` / `from logging import ...` / `logging.getLogger(...)`
  - 任何写日志的模块必须 `from astrbot.api import logger`
"""

from __future__ import annotations

import ast
import glob
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PLUGIN_SOURCES = ("bambu_a1/**/*.py", "main.py")


def plugin_files() -> list[str]:
    files: list[str] = []
    for pattern in PLUGIN_SOURCES:
        files.extend(glob.glob(os.path.join(_ROOT, pattern), recursive=True))
    return sorted(files)


def parse(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


class TestNoStdlibLogging(unittest.TestCase):
    def test_no_stdlib_logging_import(self):
        """插件代码不得 import 内置 logging。"""
        offenders = []
        for path in plugin_files():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "logging":
                            offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "logging":
                        offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
        self.assertFalse(offenders, f"这些位置还在 import 内置 logging：{offenders}")

    def test_no_stdlib_getlogger_call(self):
        """更严一层：即使别名导入，也不允许调用 getLogger。"""
        offenders = []
        for path in plugin_files():
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", "")
                if name in ("getLogger", "basicConfig"):
                    offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}: {name}()")
        self.assertFalse(offenders, f"这些位置还在自建 logger：{offenders}")

    def test_logging_compat_module_is_gone(self):
        """被审核点名的适配层必须删除，而不是留着不用。"""
        self.assertFalse(
            os.path.exists(os.path.join(_ROOT, "bambu_a1", "logging_compat.py")),
            "bambu_a1/logging_compat.py 应已删除",
        )

    def test_main_no_longer_binds_custom_logger(self):
        with open(os.path.join(_ROOT, "main.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("bind_plugin_logger", source)
        self.assertNotIn("logging_compat", source)


class TestLoggerSource(unittest.TestCase):
    def test_modules_that_log_import_host_logger(self):
        """写日志的模块必须 `from astrbot.api import logger`（审核规范）。"""
        expected = [
            "bambu_a1/auth.py",
            "bambu_a1/adapters/bambu_a1.py",
            "bambu_a1/adapters/dryer_esp32.py",
        ]
        for rel in expected:
            path = os.path.join(_ROOT, rel)
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn(
                "from astrbot.api import logger",
                source,
                f"{rel} 必须从 astrbot.api 导入 logger",
            )

    def test_host_logger_is_importable_in_tests(self):
        """单测环境用 stub 宿主，也应当能拿到同一个入口。"""
        from astrbot.api import logger  # noqa: PLC0415

        logger.info("logging compliance probe")
        logger.warning("logging compliance probe")

    def test_stub_host_logger_matches_contract(self):
        """stub 必须提供和真实宿主一致的 logger 门面（info/warning/error…）。"""
        from astrbot.api import logger  # noqa: PLC0415

        for method in ("debug", "info", "warning", "error", "exception", "critical"):
            self.assertTrue(callable(getattr(logger, method, None)), f"缺少 {method}()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
