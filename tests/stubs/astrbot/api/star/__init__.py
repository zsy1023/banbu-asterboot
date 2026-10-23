"""astrbot.api.star 的最小实现。"""

from __future__ import annotations

import logging
from typing import Any


class Context:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.provider_id = "stub-provider"
        self.llm_reply = "模型补写的一段话"
        self.llm_calls: list[dict[str, Any]] = []

    async def send_message(self, session: str, message_chain: Any) -> bool:
        self.sent.append((session, message_chain))
        return True

    async def get_current_chat_provider_id(self, umo: str = "") -> str:
        return self.provider_id

    async def llm_generate(self, **kwargs: Any):
        self.llm_calls.append(kwargs)

        class _Resp:
            completion_text = self.llm_reply

        return _Resp()


class Star:
    name = "astrbot_plugin_bambu_a1"

    def __init__(self, context: Context, config: dict | None = None) -> None:
        self.context = context
        self.logger = logging.getLogger("astrbot-stub.plugin")

    async def initialize(self) -> None:  # pragma: no cover - 由测试覆写
        return None

    async def terminate(self) -> None:  # pragma: no cover
        return None


__all__ = ["Context", "Star"]
