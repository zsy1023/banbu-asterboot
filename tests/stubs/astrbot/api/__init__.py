"""astrbot.api 的最小实现。"""

from __future__ import annotations

import logging

logger = logging.getLogger("astrbot-stub")


class AstrBotConfig(dict):
    """真实实现是 dict 子类 + save_config()。"""

    def __init__(self, data=None, path: str = ""):
        super().__init__(data or {})
        self.path = path
        self.saved = 0

    def save_config(self) -> None:  # noqa: D401
        self.saved += 1


__all__ = ["AstrBotConfig", "logger"]
