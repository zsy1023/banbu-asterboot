"""设备适配器抽象：把"拉"和"推"两种数据源分开。

A1 用 MQTT（可 poll 可 stream）；二期烘干箱会用 WebSocket 主动推送（PushSource），
所以监控引擎不能假设设备只能被轮询。见 ARCHITECTURE.md 10.2 节。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..models import ActionResult, DeviceAction, Snapshot


@runtime_checkable
class PollSource(Protocol):
    """我们主动去问（A1 的 pushall、烘干箱的 TCP STATUS / GET /api/status）。"""

    async def fetch(self) -> dict[str, Any] | None: ...


@runtime_checkable
class PushSource(Protocol):
    """设备主动送（A1 的 report 增量、烘干箱二期的 WebSocket 长连接）。"""

    def stream(self) -> AsyncIterator[dict[str, Any]]: ...


class DeviceAdapter(ABC):
    """一个设备类型的能力边界。上层只认这个接口，不认具体协议。"""

    key: str = "base"
    display_name: str = "设备"
    capabilities: set[str] = set()
    available: bool = True
    """适配器是否已实现（二期骨架置 False，注册但不装作可用）。"""

    @abstractmethod
    async def probe(self) -> bool:
        """连通性探测（不产生副作用）。"""

    @abstractmethod
    def poll_source(self) -> PollSource | None:
        """返回拉取源；只能推的设备返回 None。"""

    @abstractmethod
    def push_source(self) -> PushSource | None:
        """返回推送源；只能拉的设备返回 None。"""

    @abstractmethod
    def to_snapshot(self, raw: dict[str, Any]) -> Snapshot:
        """把原始报文转成统一快照。"""

    @abstractmethod
    async def execute(self, action: DeviceAction) -> ActionResult:
        """执行控制动作（只开放安全子集）。"""

    def event_sources(self) -> list[str]:
        """该设备能产出的事件类型（用于订阅过滤）。"""
        return []

    def is_available(self) -> bool:
        return True


# ── 注册表 ──────────────────────────────────────────────
_REGISTRY: dict[str, type[DeviceAdapter]] = {}


def register_adapter(cls: type[DeviceAdapter]) -> type[DeviceAdapter]:
    _REGISTRY[cls.key] = cls
    return cls


def get_adapter_cls(key: str) -> type[DeviceAdapter] | None:
    return _REGISTRY.get(key)


def list_adapters() -> dict[str, type[DeviceAdapter]]:
    return dict(_REGISTRY)
