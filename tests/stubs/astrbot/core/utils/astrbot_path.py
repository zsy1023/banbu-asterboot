"""路径工具 stub：数据目录可由环境变量指定，方便测试隔离。"""

from __future__ import annotations

import os
import tempfile


def get_astrbot_data_path() -> str:
    return os.environ.get("ASTRBOT_STUB_DATA_DIR") or os.path.join(
        tempfile.gettempdir(), "astrbot-stub-data"
    )


def get_astrbot_plugin_data_path() -> str:
    return os.path.join(get_astrbot_data_path(), "plugin_data")


def get_astrbot_plugin_path() -> str:
    return os.path.join(get_astrbot_data_path(), "plugins")


def get_astrbot_config_path() -> str:
    return os.path.join(get_astrbot_data_path(), "config")


__all__ = [
    "get_astrbot_config_path",
    "get_astrbot_data_path",
    "get_astrbot_plugin_data_path",
    "get_astrbot_plugin_path",
]
