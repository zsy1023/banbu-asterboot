"""拓竹 A1 适配器：MQTT 拉取 + 长连接，控制指令走同一通道。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable

from .. import mqtt
from ..compat import Profile
from ..models import ActionResult, DeviceAction, Snapshot
from .base import DeviceAdapter, PollSource, PushSource, register_adapter

logger = logging.getLogger("astrbot.plugin.bambu_a1.adapter")


class _BambuPollSource(PollSource):
    def __init__(self, adapter: "BambuA1Adapter"):
        self._adapter = adapter

    async def fetch(self) -> dict[str, Any] | None:
        return await self._adapter.fetch_raw()


class _BambuPushSource(PushSource):
    def __init__(self, adapter: "BambuA1Adapter"):
        self._adapter = adapter

    def stream(self) -> AsyncIterator[dict[str, Any]]:
        return self._adapter.stream_raw()


@register_adapter
class BambuA1Adapter(DeviceAdapter):
    key = "bambu_a1"
    display_name = "拓竹 A1"
    capabilities = {"status", "control.light", "control.pause", "control.resume", "control.stop"}

    def __init__(
        self,
        profile: Profile,
        account_provider: Callable[[], dict[str, Any]],
        config_provider: Callable[[str, Any], Any] | None = None,
    ):
        self.profile = profile
        self._account_provider = account_provider
        self._config_provider = config_provider or (lambda _k, d: d)
        self._stream: mqtt.MqttStream | None = None
        self._last_error = ""

    # ── 凭据 ────────────────────────────────────────────────
    def _account(self) -> dict[str, Any]:
        return self._account_provider() or {}

    def _mqtt_kwargs(self) -> dict[str, Any] | None:
        account = self._account()
        token = account.get("token") or ""
        username = account.get("username") or ""
        serial = account.get("serial") or ""
        if not (token and username and serial):
            return None
        region = account.get("region") or "China"
        mqtt_cfg = self.profile.mqtt
        return {
            "broker": self.profile.mqtt_broker(region),
            "port": int(mqtt_cfg.get("port", 8883)),
            "username": username,
            "password": token,
            "serial": serial,
            "report_topic": self.profile.mqtt_topic("topic_report", serial),
            "request_topic": self.profile.mqtt_topic("topic_request", serial),
            "start_cmd": self.profile.mqtt_command("cmd_start_push"),
            "pushall_cmd": self.profile.mqtt_command("cmd_push_all"),
        }

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── 数据源 ──────────────────────────────────────────────
    async def fetch_raw(self) -> dict[str, Any] | None:
        kwargs = self._mqtt_kwargs()
        if not kwargs:
            self._last_error = "尚未绑定账号或缺少序列号"
            return None
        timeout = int(self._config_provider("mqtt_timeout", 15) or 15)
        retries = int(self._config_provider("mqtt_retries", 2) or 0)
        raw = await asyncio.to_thread(
            mqtt.fetch_with_retry,
            retries=retries,
            timeout=timeout,
            **kwargs,
        )
        if not raw:
            self._last_error = "MQTT 未返回数据"
            return None
        if "__error__" in raw:
            self._last_error = str(raw["__error__"])
            return None
        self._last_error = ""
        return {"print": raw}

    async def stream_raw(self) -> AsyncIterator[dict[str, Any]]:
        kwargs = self._mqtt_kwargs()
        if not kwargs:
            self._last_error = "尚未绑定账号或缺少序列号"
            return
        stream = mqtt.MqttStream(
            broker=kwargs["broker"],
            port=kwargs["port"],
            username=kwargs["username"],
            password=kwargs["password"],
            serial=kwargs["serial"],
            report_topic=kwargs["report_topic"],
            request_topic=kwargs["request_topic"],
            start_cmd=kwargs["start_cmd"],
            pushall_cmd=kwargs["pushall_cmd"],
            emit_interval=float(self._config_provider("stream_emit_interval", 3.0) or 3.0),
        )
        self._stream = stream
        stream.start(asyncio.get_running_loop())
        try:
            async for raw in stream.stream():
                yield {"print": raw}
        finally:
            stream.stop()
            self._stream = None

    def stop_stream(self) -> None:
        if self._stream:
            self._stream.stop()

    # ── 适配器接口 ──────────────────────────────────────────
    async def probe(self) -> bool:
        return self._mqtt_kwargs() is not None

    def poll_source(self) -> PollSource | None:
        return _BambuPollSource(self)

    def push_source(self) -> PushSource | None:
        return _BambuPushSource(self)

    def to_snapshot(self, raw: dict[str, Any]) -> Snapshot:
        payload = (raw or {}).get("print", raw) or {}
        return self.profile.to_snapshot({"print": payload}, device=self.key)

    def event_sources(self) -> list[str]:
        return [
            "print_started", "print_paused", "print_resumed", "print_finished",
            "print_failed", "hms_error", "filament_runout", "milestone",
            "offline", "online",
        ]

    async def execute(self, action: DeviceAction) -> ActionResult:
        kwargs = self._mqtt_kwargs()
        if not kwargs:
            return ActionResult(ok=False, message="尚未绑定账号或缺少序列号")

        cmd_key = {
            "light": None,   # 由 params.mode 决定
            "pause": "cmd_pause",
            "resume": "cmd_resume",
            "stop": "cmd_stop",
        }.get(action.kind)
        if action.kind == "light":
            mode = str(action.params.get("mode") or "").lower()
            cmd_key = "cmd_light_on" if mode == "on" else "cmd_light_off"
        if not cmd_key:
            return ActionResult(ok=False, message=f"不支持的动作：{action.kind}")

        payload = self.profile.mqtt_command(cmd_key)
        ok = await asyncio.to_thread(
            mqtt.send_command,
            broker=kwargs["broker"],
            port=kwargs["port"],
            username=kwargs["username"],
            password=kwargs["password"],
            request_topic=kwargs["request_topic"],
            payload=payload,
        )
        if ok:
            return ActionResult(ok=True, message="指令已发送（打印机异步生效）", data={"kind": action.kind})
        return ActionResult(ok=False, message="MQTT 通道不可用，指令未发出")
