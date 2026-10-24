"""读取插件自身元数据（版本 / 仓库 / 作者），不依赖 PyYAML。

为什么不直接 import yaml：那是宿主的依赖，插件在单测或极简环境里可能没有；
metadata.yaml 我们只需要几个标量字段，用最朴素的行解析就够，且不会因为
YAML 的复杂语法误判（我们的 metadata 里只有标量 + 两个短列表）。
"""

from __future__ import annotations

import os
import re
from typing import Any

_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_BLOCK_SCALARS = ("|", ">", "|-", ">-", "|+", ">+")


def parse_metadata_text(text: str) -> dict[str, str]:
    """从 metadata.yaml 文本里取顶层标量字段（忽略多行块与列表）。"""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith((" ", "\t", "#", "-")):
            continue
        match = _FIELD_RE.match(raw_line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in _BLOCK_SCALARS:
            continue  # desc 这类多行块不参与
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        result[key] = value
    return result


def read_metadata(plugin_dir: str) -> dict[str, Any]:
    """读取插件目录下的 metadata.yaml；不存在时返回空字典（绝不抛）。"""
    for filename in ("metadata.yaml", "metadata.yml"):
        path = os.path.join(plugin_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return parse_metadata_text(fh.read())
        except OSError:
            return {}
    return {}


def astrbot_version() -> str:
    """宿主版本；拿不到就返回 unknown（不引新的失败点）。"""
    try:
        from astrbot.core.config.default import VERSION
    except Exception:  # noqa: BLE001
        return "unknown"
    return str(VERSION)
