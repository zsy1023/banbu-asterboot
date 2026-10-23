"""内部事件总线：8 个扩展点，异常隔离。

任何钩子抛错都只记 warning，不影响主流程 —— 钩子是"加能力"，不能成为"单点故障"。
"""

from __future__ import annotations

import contextlib
from typing import Any, Awaitable, Callable

HOOK_NAMES = (
    "on_snapshot",       # 每次拿到（合并后的）快照
    "on_events",         # 事件检测完、推送前：可改 channel / 注入文案 / 抑制
    "on_before_send",    # 提交给平台前：可改写文本、加前缀、换渠道
    "on_bind_success",   # 绑定成功
    "on_unbind",         # 解绑
    "on_llm_draft",      # LLM 草案返回后：可二次校验/替换
    "on_adapter_registered",
    "on_monitor_error",
)

Hook = Callable[..., Awaitable[Any] | Any]


class HookBus:
    def __init__(self, logger: Any = None):
        self._hooks: dict[str, list[Hook]] = {name: [] for name in HOOK_NAMES}
        self._log = logger

    def register(self, name: str, hook: Hook) -> bool:
        if name not in self._hooks:
            return False
        self._hooks[name].append(hook)
        return True

    def unregister(self, name: str, hook: Hook) -> bool:
        if name not in self._hooks:
            return False
        try:
            self._hooks[name].remove(hook)
            return True
        except ValueError:
            return False

    def hooks_for(self, name: str) -> list[Hook]:
        return list(self._hooks.get(name) or [])

    async def emit(self, name: str, *args: Any, **kwargs: Any) -> list[Any]:
        """依次调用并收集返回值（返回值语义由调用方解释）。"""
        results: list[Any] = []
        for hook in self.hooks_for(name):
            try:
                out = hook(*args, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                results.append(out)
            except Exception as exc:  # noqa: BLE001
                if self._log:
                    with contextlib.suppress(Exception):
                        self._log.warning("钩子 %s 执行失败: %s: %s", name, type(exc).__name__, exc)
        return results

    async def emit_first(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """取第一个非 None 的返回值（用于可替换对象的钩子）。"""
        for value in await self.emit(name, *args, **kwargs):
            if value is not None:
                return value
        return None
