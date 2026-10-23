"""持久化：原子写 JSON、轮转 jsonl、进程内锁。不依赖框架。"""

from __future__ import annotations

import json
import os
import shutil
import threading
from typing import Any

_LOCK = threading.RLock()


def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def atomic_write_text(path: str, text: str) -> None:
    """tmp + os.replace，避免半截文件。"""
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp"
    with _LOCK:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def read_json(path: str, default: Any) -> Any:
    """读 JSON；文件缺失或损坏返回 default（损坏时另存 .bak 便于排查）。"""
    if not os.path.exists(path):
        return default
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            try:
                shutil.copy2(path, f"{path}.bak")
            except OSError:
                pass
            return default


def write_json(path: str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def append_jsonl(path: str, record: dict[str, Any], max_bytes: int = 5 << 20, backups: int = 2) -> None:
    """追加一行 JSON；超过 max_bytes 时轮转（path.1 … path.N）。"""
    ensure_dir(os.path.dirname(path))
    with _LOCK:
        try:
            if os.path.exists(path) and os.path.getsize(path) >= max_bytes:
                for i in range(backups, 0, -1):
                    src = path if i == 1 else f"{path}.{i - 1}"
                    dst = f"{path}.{i}"
                    if os.path.exists(src):
                        os.replace(src, dst)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def tail_jsonl(path: str, limit: int = 50) -> list[dict[str, Any]]:
    """读最后 limit 行（文件可能不存在）。"""
    if not os.path.exists(path):
        return []
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class JsonFile:
    """一个 JSON 文件的小包装：懒加载 + 变更即落盘。"""

    def __init__(self, path: str, default: Any):
        self.path = path
        self._default = default
        self._cache: Any = None

    def load(self) -> Any:
        if self._cache is None:
            self._cache = read_json(self.path, self._default())
        return self._cache

    def save(self) -> None:
        if self._cache is not None:
            write_json(self.path, self._cache)

    def set(self, value: Any) -> None:
        self._cache = value
        self.save()
