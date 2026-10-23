"""最小 AstrBot 宿主 stub：只为本地验证插件加载与命令注册。

只实现 main.py 真正用到的接口：
  astrbot.api            → logger, AstrBotConfig
  astrbot.api.event      → AstrMessageEvent, MessageChain, filter
  astrbot.api.star       → Star, Context
  astrbot.core.utils.astrbot_path → get_astrbot_plugin_data_path
"""

from __future__ import annotations

import logging

__all__ = ["api", "core"]

logging.getLogger("astrbot-stub").addHandler(logging.NullHandler())
