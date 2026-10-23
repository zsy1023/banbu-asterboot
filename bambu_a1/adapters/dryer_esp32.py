"""烘干箱适配器骨架（二期）。

本期只登记契约，所有方法抛 NotImplementedError —— 这样配置里写了 dryer 也不会让
主流程崩，只是明确告诉你"还没做"。契约来自 3D-dryer-for-esp32-c3：

  TCP 8080 行协议：
    AUTH <key> / STATUS / START / STOP / SETTEMP <0-100> / SETTIME <1-999>
    MANUAL ON|OFF / HEATER ON|OFF / FAN ON|OFF / SINGLESENSOR ON|OFF / RESTART
  STATUS 返回：
    {"boxTemp","heaterTemp","state","targetTemp","dryTime","remaining",
     "heater","fan","fault","manual","singleSensor"}
  HTTP：
    GET  /api/status
    POST /api/control, /api/manual

二期实现要点（TODO）：
  1. PushSource：WebSocket 长连接（设备主动推送），含 auth / 心跳 / 断线重连
  2. PollSource：兜底，TCP 8080 STATUS 或 HTTP /api/status
  3. 字段映射走 protocol_profiles/dryer_v1.json（boxTemp→chamber …）
  4. 事件：drying_started / drying_done / overheat / sensor_fault / cooling
  5. 与 A1 联动：print_finished → 按 dryer.require_confirm 询问或直接开始
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from ..logging_compat import get_logger
from ..models import ActionResult, DeviceAction, Snapshot
from .base import DeviceAdapter, PollSource, PushSource, register_adapter

logger = get_logger("dryer")

NOT_IMPLEMENTED_MSG = "烘干箱适配器尚未实现（二期）：接口与配置位已就绪，逻辑待填"


@register_adapter
class DryerEsp32Adapter(DeviceAdapter):
    key = "dryer_esp32"
    display_name = "3D 烘干箱"
    capabilities = {"status", "control.dry_start", "control.dry_stop", "control.target_temp"}
    available = False  # 二期实现后置 True

    def __init__(self, ws_url: str = "", tcp_host: str = "", api_key: str = ""):
        self.ws_url = ws_url
        self.tcp_host = tcp_host
        self.api_key = api_key

    def is_available(self) -> bool:
        return False  # 二期置 True

    async def probe(self) -> bool:
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    def poll_source(self) -> PollSource | None:
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    def push_source(self) -> PushSource | None:
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    def to_snapshot(self, raw: dict[str, Any]) -> Snapshot:
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    async def execute(self, action: DeviceAction) -> ActionResult:
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    def event_sources(self) -> list[str]:
        return ["drying_started", "drying_done", "overheat", "sensor_fault"]


async def stream_placeholder() -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
    """二期的 WebSocket 推送入口占位。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MSG)
    yield {}  # pragma: no cover - 让静态检查知道这是异步生成器
