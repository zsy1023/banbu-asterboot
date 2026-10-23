"""日志适配层：把插件内部日志统一交给 AstrBot 的插件 logger。

**为什么需要这一层（真实事故）**：早期实现里各模块用
`logging.getLogger("astrbot.plugin.bambu_a1.auth")` 直接建 logger。
这个名字落在宿主的 `astrbot.*` 命名空间里，但并不是通过 `LogManager` 创建的，
所以没有 `_RecordEnricherFilter` 注入的 `plugin_tag` 等字段。
记录向上冒泡到宿主 `astrbot` logger 上的 `LogQueueHandler`（格式串含 `%(plugin_tag)s`），
`self.format(record)` 直接抛 `ValueError: Formatting field not found in record: 'plugin_tag'`
—— 一条 INFO 日志把 `/banbu bind` 整个命令打挂了。

三条规则：
1. 绝不自己创建 `astrbot.*` 名字的 logger；
2. 插件实例化后调用 `bind_plugin_logger(self.logger)`，之后所有模块日志都走宿主的插件 logger
   （在 WebUI 日志面板里带正确的 `[插件名]` 标签，也能按插件调日志级别）；
3. 绑定之前（模块导入早期、单测环境）回退到独立的 `bambu_a1` logger，
   名字不在 `astrbot.*` 下，宿主 root 的 loguru 拦截器用 `getattr(record, "plugin_tag", "[Core]")`
   兜底，不会炸。

另外 `_Facade` 会吞掉日志系统自身的异常：**日志永远不能影响业务**。
"""

from __future__ import annotations

import logging
from typing import Any

#: 回退 logger 名；刻意不以 "astrbot." 开头
FALLBACK_LOGGER_NAME = "bambu_a1"

_plugin_logger: logging.Logger | None = None
_fallback_logger = logging.getLogger(FALLBACK_LOGGER_NAME)
_warned: set[str] = set()


def bind_plugin_logger(logger: logging.Logger | None) -> None:
    """由插件入口在 `__init__` 里调用，把宿主提供的插件 logger 交给本层。"""
    global _plugin_logger
    _plugin_logger = logger


def get_plugin_logger() -> logging.Logger | None:
    return _plugin_logger


def _resolve() -> logging.Logger:
    return _plugin_logger if _plugin_logger is not None else _fallback_logger


class _Facade:
    """模块级 `logger` 对象：前缀带上模块名，异常一律吞掉。"""

    __slots__ = ("suffix",)

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix

    # ── 内部 ────────────────────────────────────────────────
    def _emit(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        try:
            target = _resolve()
            if self.suffix:
                target.log(level, f"[{self.suffix}] {msg}", *args, **kwargs)
            else:
                target.log(level, msg, *args, **kwargs)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响业务
            key = f"{self.suffix}:{level}"
            if key not in _warned:
                _warned.add(key)
                # 用最朴素的方式留个痕迹，且不再经过任何 handler
                try:
                    import sys

                    print(
                        f"[bambu_a1] 日志系统异常，已忽略（{self.suffix}）",
                        file=sys.stderr,
                    )
                except Exception:  # noqa: BLE001
                    pass

    # ── 兼容 logging.Logger 的常用接口 ──────────────────────
    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, *args, **kwargs)

    warn = warning

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, msg, *args, **kwargs)

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(level, msg, *args, **kwargs)

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - 与 logging 保持一致
        try:
            return _resolve().isEnabledFor(level)
        except Exception:  # noqa: BLE001
            return False


_CACHE: dict[str, _Facade] = {}


def get_logger(suffix: str = "") -> _Facade:
    """取某个模块的日志门面（同名复用同一个对象）。"""
    if suffix not in _CACHE:
        _CACHE[suffix] = _Facade(suffix)
    return _CACHE[suffix]
