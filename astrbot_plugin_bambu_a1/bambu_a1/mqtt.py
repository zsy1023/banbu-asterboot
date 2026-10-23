"""MQTT 通道：一次性拉取（poll）+ 长连接增量（stream）。

拓竹 broker 的现状（见 ARCHITECTURE.md 2.2 节）：
  - 账号 username=u_{uid}，password=accessToken
  - 证书链不被标准库接受，必须 CERT_NONE + tls_insecure
  - 订阅 device/{serial}/report，向 device/{serial}/request 发指令
  - pushall 返回全量，后续增量报文只带变化字段 → 必须深合并

paho 是阻塞库，所有调用都在线程里跑，asyncio 侧只拿队列，绝不在事件循环里 sleep。
"""

from __future__ import annotations

import asyncio
import copy
import json
import ssl
import threading
import time
from typing import Any, AsyncIterator

import paho.mqtt.client as mqtt

from .models import deep_merge

# paho-mqtt 2.x 需要显式指定回调 API 版本
try:
    _CALLBACK_API = mqtt.CallbackAPIVersion.VERSION2
except AttributeError:  # pragma: no cover - paho 1.x
    _CALLBACK_API = None


def _new_client(client_id: str = "") -> mqtt.Client:
    if _CALLBACK_API is not None:
        return mqtt.Client(_CALLBACK_API, client_id=client_id) if client_id else mqtt.Client(_CALLBACK_API)
    return mqtt.Client(client_id=client_id) if client_id else mqtt.Client()


def _apply_tls(client: mqtt.Client, insecure: bool = True) -> None:
    client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    if insecure:
        client.tls_insecure_set(True)


def _is_full_report(payload: dict[str, Any]) -> bool:
    """粗略判定是否为全量报文：字段多、带 gcode_state。"""
    node = payload.get("print")
    return isinstance(node, dict) and len(node) > 10 and "gcode_state" in node


# ─────────────────────────────── poll ───────────────────────────────


def fetch_once(
    *,
    broker: str,
    port: int,
    username: str,
    password: str,
    serial: str,
    report_topic: str,
    request_topic: str,
    start_cmd: dict[str, Any],
    pushall_cmd: dict[str, Any],
    timeout: int = 15,
    connect_timeout: int = 60,
) -> dict[str, Any]:
    """连接一次、取一份（尽量完整的）状态，返回 {"print": {...}} 里的 print 子树。"""
    merged: dict[str, Any] = {}
    done = threading.Event()
    errors: list[str] = []

    def on_connect(client, userdata, flags, rc, properties=None):
        code = int(getattr(rc, "value", rc) or 0)
        if code != 0:
            errors.append(f"connect rc={code}")
            done.set()
            return
        client.subscribe(report_topic)
        client.publish(request_topic, json.dumps(start_cmd))
        client.publish(request_topic, json.dumps(pushall_cmd))

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        node = payload.get("print")
        if not isinstance(node, dict):
            return
        deep_merge(merged, node)
        if _is_full_report(payload):
            done.set()

    def on_disconnect(client, userdata, *args):
        if not done.is_set():
            errors.append("disconnected before full report")
        done.set()

    client = _new_client()
    client.username_pw_set(username, password=password)
    _apply_tls(client)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(broker, port, connect_timeout)
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}

    deadline = time.time() + timeout
    try:
        while not done.is_set() and time.time() < deadline:
            client.loop(timeout=1.0)
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    if not merged and errors:
        return {"__error__": "; ".join(errors[-3:])}
    return merged


def fetch_with_retry(*, retries: int = 2, retry_delay: float = 3.0, **kwargs: Any) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(retry_delay)
        last = fetch_once(**kwargs)
        if last and "__error__" not in last:
            return last
    return last


# ────────────────────────────── stream ──────────────────────────────


class MqttStream:
    """长连接：常驻线程 + asyncio 队列，增量合并后按间隔产出快照。"""

    def __init__(
        self,
        *,
        broker: str,
        port: int,
        username: str,
        password: str,
        serial: str,
        report_topic: str,
        request_topic: str,
        start_cmd: dict[str, Any],
        pushall_cmd: dict[str, Any],
        emit_interval: float = 3.0,
    ):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.serial = serial
        self.report_topic = report_topic
        self.request_topic = request_topic
        self.start_cmd = start_cmd
        self.pushall_cmd = pushall_cmd
        self.emit_interval = emit_interval

        self._buf: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_emit = 0.0
        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._connected = False
        self._last_error = ""
        self._stop = threading.Event()

    # ── 生命周期 ────────────────────────────────────────────
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bambu-mqtt-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── 线程侧 ──────────────────────────────────────────────
    def _run(self) -> None:
        client = _new_client(client_id=f"bambu-a1-{self.serial[-6:]}")
        self._client = client
        client.username_pw_set(self.username, password=self.password)
        _apply_tls(client)
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        try:
            client.connect_async(self.broker, self.port, 60)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._connected = False
            try:
                client.loop_stop()
            except Exception:  # noqa: BLE001
                pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        code = int(getattr(rc, "value", rc) or 0)
        if code != 0:
            self._last_error = f"connect rc={code}"
            self._connected = False
            return
        self._connected = True
        self._last_error = ""
        client.subscribe(self.report_topic)
        client.publish(self.request_topic, json.dumps(self.start_cmd))
        client.publish(self.request_topic, json.dumps(self.pushall_cmd))

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        node = payload.get("print")
        if not isinstance(node, dict):
            return
        with self._lock:
            deep_merge(self._buf, node)
            self._dirty = True
            should_emit = _is_full_report(payload) or (
                time.time() - self._last_emit >= self.emit_interval
            )
            snapshot = copy.deepcopy(self._buf) if should_emit else None
            if should_emit:
                self._dirty = False
                self._last_emit = time.time()
        if snapshot is not None:
            self._push(snapshot)

    def _push(self, snapshot: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._offer, snapshot)

    def _offer(self, snapshot: dict[str, Any]) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()  # 丢掉最旧的，保最新
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            pass

    # ── asyncio 侧 ──────────────────────────────────────────
    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue


# ────────────────────────────── 控制 ──────────────────────────────


def send_command(
    *,
    broker: str,
    port: int,
    username: str,
    password: str,
    request_topic: str,
    payload: dict[str, Any],
    timeout: float = 10.0,
) -> bool:
    """发一条控制指令，不等响应。"""
    sent = threading.Event()
    ok_flag = {"ok": False}

    def on_connect(client, userdata, flags, rc, properties=None):
        code = int(getattr(rc, "value", rc) or 0)
        if code != 0:
            sent.set()
            return
        client.publish(request_topic, json.dumps(payload))
        ok_flag["ok"] = True
        sent.set()

    client = _new_client()
    client.username_pw_set(username, password=password)
    _apply_tls(client)
    client.on_connect = on_connect
    try:
        client.connect(broker, port, 60)
        deadline = time.time() + timeout
        while not sent.is_set() and time.time() < deadline:
            client.loop(timeout=1.0)
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
    return bool(ok_flag["ok"])
