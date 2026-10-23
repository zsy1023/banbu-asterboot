"""astrbot.api.event 的最小实现：事件对象、消息链、注册器。"""

from __future__ import annotations

from typing import Any, Callable

# 记录注册结果，供冒烟测试断言
GROUPS: dict[str, "_Group"] = {}
TOOLS: dict[str, Callable[..., Any]] = {}
COMMANDS: dict[str, Callable[..., Any]] = {}


class _Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class MessageChain:
    def __init__(self) -> None:
        self.chain: list[Any] = []

    def message(self, text: str) -> "MessageChain":
        self.chain.append(_Plain(text))
        return self

    def get_plain_text(self) -> str:
        return "".join(c.text for c in self.chain if isinstance(c, _Plain))


class MessageEventResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.chain = [_Plain(text)]


class AstrMessageEvent:
    def __init__(self, message_str: str = "", umo: str = "stub:FriendMessage:1") -> None:
        self.message_str = message_str
        self.unified_msg_origin = umo
        self._sender = "测试用户"

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult(text)

    def get_sender_name(self) -> str:
        return self._sender

    def get_platform_name(self) -> str:
        return "stub"


class _Group:
    """对应真实实现里的 RegisteringCommandable。"""

    def __init__(self, name: str, fn: Callable[..., Any]) -> None:
        self.name = name
        self.fn = fn
        self.commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str, alias: set[str] | None = None, **kwargs: Any):
        def decorator(awaitable):
            self.commands[name] = awaitable
            COMMANDS[f"{self.name} {name}"] = awaitable
            return awaitable

        return decorator


class _Filter:
    EventMessageType = type("EventMessageType", (), {"ALL": 0, "GROUP_MESSAGE": 1, "PRIVATE_MESSAGE": 2})
    PlatformAdapterType = type("PlatformAdapterType", (), {"ALL": 0})
    PermissionType = type("PermissionType", (), {"ADMIN": 0})

    def command_group(self, name: str | None = None, alias: set[str] | None = None, **kwargs: Any):
        def decorator(fn):
            group = _Group(name or fn.__name__, fn)
            GROUPS[name or fn.__name__] = group
            return group

        return decorator

    def command(self, name: str, **kwargs: Any):
        def decorator(awaitable):
            COMMANDS[name] = awaitable
            return awaitable

        return decorator

    def llm_tool(self, name: str | None = None, **kwargs: Any):
        def decorator(awaitable):
            TOOLS[name or awaitable.__name__] = awaitable
            return awaitable

        return decorator

    # 其余装饰器在 stub 里都退化为"原样返回"
    def __getattr__(self, item: str):
        def decorator(*args: Any, **kwargs: Any):
            if args and callable(args[0]) and len(args) == 1:
                return args[0]

            def inner(fn):
                return fn

            return inner

        return decorator


filter = _Filter()

__all__ = [
    "AstrMessageEvent",
    "COMMANDS",
    "GROUPS",
    "MessageChain",
    "MessageEventResult",
    "TOOLS",
    "filter",
]
