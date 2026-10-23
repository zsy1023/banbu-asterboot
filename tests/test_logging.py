"""日志兼容层回归测试。

事故背景：插件内部曾用 `logging.getLogger("astrbot.plugin.bambu_a1.*")` 自建 logger，
记录冒泡到宿主 `astrbot` logger 的 `LogQueueHandler`（格式串含 `%(plugin_tag)s`）时，
因为没走 `LogManager` 的 `_RecordEnricherFilter`，`self.format(record)` 抛
`ValueError: Formatting field not found in record: 'plugin_tag'`，
把 `/banbu bind` 直接打挂。这里的断言保证不会再犯。
"""

from __future__ import annotations

import glob
import logging
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bambu_a1.logging_compat import (  # noqa: E402
    FALLBACK_LOGGER_NAME,
    bind_plugin_logger,
    get_logger,
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Exploding(logging.Handler):
    """模拟宿主那种不走 handleError、异常直接抛出的自定义 handler。"""

    def emit(self, record: logging.LogRecord) -> None:
        raise ValueError("Formatting field not found in record: 'plugin_tag'")


def _make_logger(name: str, handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


class TestNoAstrbotLoggerNamespace(unittest.TestCase):
    def test_sources_never_create_astrbot_loggers(self):
        """绝不能在 astrbot.* 命名空间里自建 logger（事故根因）。

        用 AST 只看真实调用，避免把文档字符串里的示例文本算成违规。
        """
        import ast

        offenders = []
        files: list[str] = []
        for pattern in ("bambu_a1/**/*.py", "main.py"):
            files.extend(glob.glob(os.path.join(_ROOT, pattern), recursive=True))
        for path in files:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", "")
                if name != "getLogger":
                    continue
                arg = node.args[0]
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value.startswith("astrbot")
                ):
                    offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno} -> {arg.value}")
        self.assertFalse(offenders, f"这些位置自建了 astrbot.* logger：{offenders}")

    def test_fallback_logger_name_is_outside_astrbot(self):
        self.assertFalse(FALLBACK_LOGGER_NAME.startswith("astrbot"))

    def test_main_binds_host_plugin_logger(self):
        with open(os.path.join(_ROOT, "main.py"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("bind_plugin_logger(self.logger)", text)


class TestLogFacade(unittest.TestCase):
    def tearDown(self) -> None:
        bind_plugin_logger(None)

    def test_records_go_to_bound_logger_with_module_prefix(self):
        capture = _Capture()
        bind_plugin_logger(_make_logger("test.bambu_a1.bound", capture))

        log = get_logger("auth")
        log.info("验证码已发送到 %s", "138****1111")
        log.warning("注意")

        self.assertEqual(len(capture.records), 2)
        message = capture.records[0].getMessage()
        self.assertEqual(message, "[auth] 验证码已发送到 138****1111")
        self.assertEqual(capture.records[0].levelno, logging.INFO)

    def test_bound_logger_may_enrich_plugin_tag(self):
        """宿主 logger 的 filter 会补 plugin_tag；这里验证记录确实经过绑定 logger。"""
        capture = _Capture()

        class _Enricher(logging.Filter):
            def filter(self, record):
                record.plugin_tag = "[demo]"
                return True

        logger = _make_logger("test.bambu_a1.enriched", capture)
        logger.addFilter(_Enricher())
        bind_plugin_logger(logger)

        get_logger("mqtt").info("hello")
        self.assertEqual(getattr(capture.records[0], "plugin_tag", None), "[demo]")

    def test_handler_exception_never_breaks_business(self):
        """宿主 handler 抛异常时，日志调用本身不能把业务流程打挂。"""
        bind_plugin_logger(_make_logger("test.bambu_a1.exploding", _Exploding()))
        log = get_logger("auth")
        # 不应抛出
        log.info("请求短信验证码: %s", "177****5191")
        log.error("仍然不能抛")
        log.exception("带堆栈也不能抛")

    def test_unbound_state_still_logs(self):
        bind_plugin_logger(None)
        log = get_logger("coalescer")
        log.debug("没有宿主 logger 时也不应报错")

    def test_get_logger_is_cached(self):
        self.assertIs(get_logger("cache_probe"), get_logger("cache_probe"))


class TestHostStyleLogging(unittest.TestCase):
    """完全按 AstrBot 4.29.1 的机制复现一遍：严格 formatter + 插件 logger 的 enrich filter。"""

    def tearDown(self) -> None:
        bind_plugin_logger(None)

    def test_module_log_survives_strict_host_formatter(self):
        import io

        class _EnrichFilter(logging.Filter):
            def filter(self, record):
                record.plugin_tag = f"[{record.name.rsplit('.', 1)[-1]}]"
                record.short_levelname = record.levelname[:4]
                return True

        class _StrictFormatter(logging.Formatter):
            def format(self, record):
                if not hasattr(record, "plugin_tag"):
                    raise ValueError("Formatting field not found in record: 'plugin_tag'")
                return super().format(record)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_StrictFormatter("%(plugin_tag)s %(message)s"))

        host_logger = logging.getLogger("astrbot.plugin.astrbot_plugin_bambu_a1")
        host_logger.handlers = [handler]
        host_logger.propagate = False
        host_logger.setLevel(logging.DEBUG)
        host_logger.addFilter(_EnrichFilter())

        bind_plugin_logger(host_logger)

        from bambu_a1 import auth  # 模块级 logger 已在导入时建好

        auth.logger.info("请求短信验证码: %s", "177****5191")
        auth.logger.warning("验证码请求已发出（响应异常）")

        output = stream.getvalue()
        self.assertIn("[astrbot_plugin_bambu_a1]", output)
        self.assertIn("请求短信验证码: 177****5191", output)
        self.assertIn("[auth]", output, "模块名应体现在消息前缀里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
