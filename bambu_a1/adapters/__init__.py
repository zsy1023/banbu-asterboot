"""设备适配器注册表。

导入本包即完成注册；新增设备只需新增模块并在这里 import 一次。
"""

from .base import (
    DeviceAdapter,
    PollSource,
    PushSource,
    get_adapter_cls,
    list_adapters,
    register_adapter,
)
from .bambu_a1 import BambuA1Adapter
from .dryer_esp32 import DryerEsp32Adapter

__all__ = [
    "BambuA1Adapter",
    "DeviceAdapter",
    "DryerEsp32Adapter",
    "PollSource",
    "PushSource",
    "get_adapter_cls",
    "list_adapters",
    "register_adapter",
]
