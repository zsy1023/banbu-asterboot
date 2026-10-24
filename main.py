"""AstrBot 插件入口：装配 core / ui / llm，注册命令与 LLM 工具。

分层原则（ARCHITECTURE.md 第 3 节）：本文件只做"薄封装 + 装配"，
业务判断在 bambu_a1/ 里，渲染在 render.py，路由在 router.py，模型在 bridge.py。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import sys
import time
from typing import Any

# ── 内部包导入：兼容"作为包成员加载"与"作为顶层模块加载"两种方式 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
try:  # pragma: no cover - 取决于宿主加载方式
    from .bambu_a1 import about, auth, prompts, render, router
    from .bambu_a1.adapters import BambuA1Adapter, list_adapters
    from .bambu_a1.bridge import LlmBridge
    from .bambu_a1.coalescer import Coalescer
    from .bambu_a1.compat import Profile, load_profile
    from .bambu_a1.logging_compat import bind_plugin_logger
    from .bambu_a1.credentials import (
        CryptoUnavailable,
        CredentialBox,
        DecryptError,
        generate_secret,
        mask_phone,
        mask_serial,
    )
    from .bambu_a1.events import EventState
    from .bambu_a1.hooks import HookBus
    from .bambu_a1.models import Account, DeviceAction, DeviceEvent, Snapshot
    from .bambu_a1.monitor import MonitorEngine
    from .bambu_a1.push import Pusher, Subscribers, parse_quiet_range
    from .bambu_a1.rules import RULE_TYPES, RuleEngine, parse_temp_range
    from .bambu_a1.store import JsonFile, append_jsonl, tail_jsonl
    from .bambu_a1.tools import BambuTools
except ImportError:  # 宿主把 main.py 当顶层模块加载时
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from bambu_a1 import about, auth, prompts, render, router
    from bambu_a1.adapters import BambuA1Adapter, list_adapters
    from bambu_a1.bridge import LlmBridge
    from bambu_a1.coalescer import Coalescer
    from bambu_a1.compat import Profile, load_profile
    from bambu_a1.logging_compat import bind_plugin_logger
    from bambu_a1.credentials import (
        CryptoUnavailable,
        CredentialBox,
        DecryptError,
        generate_secret,
        mask_phone,
        mask_serial,
    )
    from bambu_a1.events import EventState
    from bambu_a1.hooks import HookBus
    from bambu_a1.models import Account, DeviceAction, DeviceEvent, Snapshot
    from bambu_a1.monitor import MonitorEngine
    from bambu_a1.push import Pusher, Subscribers, parse_quiet_range
    from bambu_a1.rules import RULE_TYPES, RuleEngine, parse_temp_range
    from bambu_a1.store import JsonFile, append_jsonl, tail_jsonl
    from bambu_a1.tools import BambuTools

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_bambu_a1"
DEVICE_NAME = "拓竹 A1"
PENDING_BIND_TTL = 900  # 验证码会话 15 分钟

HELP_TEXT = """拓竹 A1 状态助手

绑定与设备
  /banbu bind <手机号>      发送短信验证码（中国区账号）
  /banbu code <验证码>      完成登录并加密保存 token
  /banbu sn <序列号>        绑定打印机并建立基线
  /banbu devices            尝试从云端列举已绑定设备
  /banbu unbind             清除凭据（需 confirm）

状态与控制
  /banbu status             当前状态卡
  /banbu monitor [on|off|auto|status]   后台监控
  /banbu light [on|off]     机箱灯
  /banbu resume             继续打印
  /banbu pause|stop confirm 暂停/停止（需 confirm）

提醒与推送
  /banbu remind add <类型> <值> [--llm]  添加提醒
  /banbu remind list|del <id>|clear      查看/删除/清空
  /banbu quiet <23:00-07:30|off>         安静时段
  /banbu milestones <25,50,75|off>       进度里程碑（默认关闭）
  /banbu subscribe|unsubscribe           本会话加入/退出推送

智能与诊断
  /banbu llm off|summary|full             LLM 总闸
  /banbu route [list|set <事件> <通道>]   分界线
  /banbu digest                          最近打印小结
  /banbu protocol                        协议档案与漂移诊断
  /banbu version                         插件版本与更新方式
  /banbu test-event <类型>               造一条事件验证推送链路

也可以直接用自然语言问我，例如"现在打得怎么样""打到一半提醒我"。"""


class BambuA1Plugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        # 先把宿主的插件 logger 交给日志适配层：bambu_a1/* 里所有模块日志都走它，
        # 避免自己建 astrbot.* logger 导致宿主格式化时缺 plugin_tag 而抛异常。
        bind_plugin_logger(self.logger)
        self.config: Any = config if isinstance(config, dict) else {}
        self.data_dir = self._resolve_data_dir()
        self.logger.info("数据目录：%s", self.data_dir)

        # ── 协议档案 ──
        profile_name = str(self.config_get("protocol_profile", "a1_v1") or "a1_v1")
        overrides = self.config_get("protocol_overrides", {}) or {}
        try:
            self.profile: Profile = load_profile(profile_name, overrides if isinstance(overrides, dict) else None)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("协议档案加载失败(%s)：%s，使用空档案继续", profile_name, exc)
            self.profile = Profile({"profile": "fallback", "fields": {}, "states": {}})

        # ── 持久化 ──
        self.account_file = JsonFile(os.path.join(self.data_dir, "account.json"), dict)
        self.subscribers = Subscribers(os.path.join(self.data_dir, "subscribers.json"))
        self.rules = RuleEngine(os.path.join(self.data_dir, "rules.json"))
        self.event_state = EventState(os.path.join(self.data_dir, "event_state.json"))
        self.events_path = os.path.join(self.data_dir, "events.jsonl")

        # ── 运行时组件 ──
        self.hooks = HookBus(self.logger)
        self._account_cache: dict[str, Any] = {}
        self.adapter = BambuA1Adapter(self.profile, self._account_for_adapter, self.config_get)
        self.monitor = MonitorEngine(
            adapter=self.adapter,
            profile=self.profile,
            rules=self.rules,
            state=self.event_state,
            config=self.config_get,
            on_snapshot=self._on_snapshot,
            on_events=self._on_events,
            on_error=self._on_monitor_error,
            logger=self.logger,
        )
        self.pusher = Pusher(
            self.context,
            self.subscribers,
            self.config_get,
            hooks=self.hooks,
            logger=self.logger,
            fallback_targets=lambda: list(self.config_get("push_targets", []) or []),
        )
        self.bridge = LlmBridge(
            self.context,
            self.config_get,
            hooks=self.hooks,
            logger=self.logger,
            history_provider=self.recent_events,
        )
        self.coalescer = Coalescer(
            self._flush_events,
            window_s=float(self.config_get("merge_window_s", 3) or 0),
            critical_window_s=float(self.config_get("merge_critical_window_s", 0.8) or 0),
            draft=self._draft_event,
            logger=self.logger,
        )
        self.tools = BambuTools(self)

        self._pending_bind: dict[str, Any] = {}
        self._confirms: dict[str, tuple[str, float]] = {}
        self._metadata_cache: dict[str, Any] | None = None

    # ─────────────────────────── 基础设施 ───────────────────────────
    def metadata(self) -> dict[str, Any]:
        """插件自身元数据（name/version/repo/author），读一次缓存。

        版本信息只用于展示与排查；**更新检测交给 AstrBot 内置的插件更新**，
        插件侧不轮询远端，避免重复实现和额外的网络请求。
        """
        if self._metadata_cache is None:
            self._metadata_cache = about.read_metadata(_HERE)
        return self._metadata_cache

    def _resolve_data_dir(self) -> str:
        base = ""
        try:  # AstrBot >= 4.9.2
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = get_astrbot_plugin_data_path()
        except Exception:  # noqa: BLE001
            base = os.path.join(os.getcwd(), "data", "plugin_data")
        name = getattr(self, "name", None) or PLUGIN_NAME
        path = os.path.join(base, name)
        os.makedirs(path, exist_ok=True)
        return path

    def config_get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    def set_config(self, key: str, value: Any) -> bool:
        if not isinstance(self.config, dict):
            return False
        self.config[key] = value
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("配置保存失败：%s", exc)
                return False
        return True

    async def initialize(self) -> None:
        self.logger.info("拓竹 A1 状态助手已加载（已绑定=%s）", self.is_bound())
        if not bool(self.config_get("enabled", True)):
            return
        if bool(self.config_get("auto_start", True)) and self.is_bound():
            await self.start_monitor()

    async def terminate(self) -> None:
        await self.stop_monitor()
        with contextlib.suppress(Exception):
            await self.coalescer.cancel_pending()
        self.logger.info("拓竹 A1 状态助手已卸载")

    # ─────────────────────────── 凭据 ───────────────────────────
    def _ensure_secret(self) -> str | None:
        secret = str(self.config_get("secret_key", "") or "")
        if secret:
            return secret
        if self.account_file.load():  # 已有凭据但主密钥丢了：不能自动换新密钥，否则解不开
            return None
        secret = generate_secret()
        self.set_config("secret_key", secret)
        self.logger.info("已自动生成凭据主密钥并写入配置")
        return secret

    def _credential_box(self) -> CredentialBox | None:
        secret = self._ensure_secret()
        if not secret:
            return None
        return CredentialBox(secret, instance_id=self.data_dir)

    def load_account(self) -> Account | None:
        data = self.account_file.load()
        if not isinstance(data, dict) or not data:
            return None
        return Account.from_dict(data)

    def is_bound(self) -> bool:
        account = self.load_account()
        return bool(account and account.serial and account.token_cipher)

    def _account_for_adapter(self) -> dict[str, Any]:
        """给适配器用的明文凭据（带内存缓存，避免每轮 scrypt）。"""
        account = self.load_account()
        if not account or not account.token_cipher:
            return {}
        cache_key = str(account.updated_at)
        cached = self._account_cache.get(cache_key)
        if cached:
            return cached
        box = self._credential_box()
        if not box:
            return {}
        try:
            token = box.decrypt(account.token_cipher)
        except DecryptError as exc:
            self.logger.error("%s", exc)
            return {}
        payload = {
            "token": token,
            "username": account.username,
            "serial": account.serial,
            "region": account.region,
        }
        self._account_cache = {cache_key: payload}
        return payload

    def _save_account(self, account: Account) -> None:
        account.updated_at = time.time()
        self.account_file.set(account.to_dict())
        self._account_cache.clear()

    # ─────────────────────────── 监控 ───────────────────────────
    async def start_monitor(self) -> bool:
        if not self.is_bound():
            return False
        await self.monitor.start()
        return True

    async def stop_monitor(self) -> None:
        await self.monitor.stop()

    def monitor_status(self) -> dict[str, Any]:
        return self.monitor.status()

    def snapshot(self) -> Snapshot | None:
        return self.event_state.last_snapshot

    def snapshot_age_s(self) -> float | None:
        snap = self.snapshot()
        return (time.time() - snap.ts) if snap else None

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        records = tail_jsonl(self.events_path, max(1, limit))
        return list(reversed(records))

    async def _on_snapshot(self, snap: Snapshot) -> None:
        await self.hooks.emit("on_snapshot", snap)

    async def _on_events(self, events: list[DeviceEvent], snap: Snapshot | None) -> None:
        # 1) 分界线：rule > llm_route > 默认，并受 llm_mode 总闸约束
        router.apply(
            events,
            llm_route=self.config_get("llm_route", {}),
            llm_mode=self.config_get("llm_mode", "summary_only"),
        )
        # 2) 允许钩子改写事件（抑制/注入文案/换通道）
        await self.hooks.emit("on_events", events, snap)
        # 3) 事件流水
        for event in events:
            with contextlib.suppress(OSError):
                append_jsonl(self.events_path, event.to_dict())
        # 4) 合并窗口（窗口值随配置变化即时生效）
        self.coalescer.window_s = float(self.config_get("merge_window_s", 3) or 0)
        self.coalescer.critical_window_s = float(self.config_get("merge_critical_window_s", 0.8) or 0)
        await self.coalescer.submit(events)

    def _on_monitor_error(self, exc: Exception, phase: str) -> None:
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(
                self.hooks.emit("on_monitor_error", exc, phase)
            )

    async def _flush_events(self, events: list[DeviceEvent]) -> None:
        await self.pusher.deliver(events)

    async def _draft_event(self, event: DeviceEvent) -> str | None:
        umo = ""
        targets = self.pusher.targets()
        if targets:
            umo = targets[0]
        return await self.bridge.draft_for_event(event, self.snapshot(), umo=umo)

    # ─────────────────────────── 控制 ───────────────────────────
    def request_confirm(self, action: str) -> str:
        token = secrets.token_hex(2)
        self._confirms[action] = (token, time.time() + 60)
        return token

    def check_confirm(self, action: str, token: str = "") -> bool:
        entry = self._confirms.get(action)
        if not entry:
            return False
        expected, expire = entry
        if time.time() > expire:
            self._confirms.pop(action, None)
            return False
        if token and token != expected:
            return False
        self._confirms.pop(action, None)
        return True

    async def execute_action(self, action: DeviceAction):
        return await self.adapter.execute(action)

    # ─────────────────────────── 绑定流程 ───────────────────────────
    async def do_bind(self, phone: str) -> str:
        phone = (phone or "").strip()
        if not phone.isdigit() or not (7 <= len(phone) <= 15):
            return "手机号看起来不对：请输入纯数字完整号码，例如 /banbu bind 13800000000"
        try:
            self._credential_box()
        except (CryptoUnavailable, ValueError) as exc:
            return f"无法加密保存凭据：{exc}"

        region = str(self.config_get("region", "China") or "China")
        result = await asyncio.to_thread(auth.send_sms, self.profile, region, phone)
        if not result.get("ok"):
            return f"发送验证码失败：{result.get('error')}"

        self._pending_bind = {
            "phone": phone,
            "region": region,
            "expire": time.time() + PENDING_BIND_TTL,
        }
        warning = str(result.get("warning") or "")
        status = int(result.get("status") or 0)
        if warning:
            return f"注意：验证码请求已发出（HTTP {status}）\n{warning}"
        extra = "" if auth.has_curl_cffi() else "\n（提示：未安装 curl_cffi，若收不到码先 pip install curl_cffi）"
        return (
            f"验证码已发送到 {mask_phone(phone)}（15 分钟内有效）。\n"
            f"收到后发送：/banbu code <验证码>{extra}"
        )

    async def do_login(self, code: str, umo: str = "") -> str:
        code = (code or "").strip()
        pending = self._pending_bind
        if not pending:
            return "还没有待验证的手机号，请先发送 /banbu bind <手机号>"
        if time.time() > float(pending.get("expire", 0)):
            self._pending_bind = {}
            return "验证码会话已过期，请重新发送 /banbu bind <手机号>"
        if not code:
            return "请带上验证码：/banbu code 123456"

        region = pending.get("region") or str(self.config_get("region", "China") or "China")
        result = await asyncio.to_thread(auth.login_with_code, self.profile, region, pending["phone"], code)
        if not result.get("ok"):
            return f"登录失败：{result.get('error')}"

        token = result["token"]
        info = await asyncio.to_thread(auth.fetch_uid, self.profile, region, token)
        if not info.get("ok"):
            return f"登录成功但拿不到账号信息：{info.get('error')}"

        box = self._credential_box()
        if not box:
            return "无法初始化加密箱，请检查 secret_key 配置"
        account = self.load_account() or Account()
        account.region = region
        account.uid = info["uid"]
        account.username = info["username"]
        account.phone_masked = mask_phone(pending["phone"])
        account.phone_cipher = box.encrypt(pending["phone"])
        account.token_cipher = box.encrypt(token)
        self._save_account(account)
        self._pending_bind = {}

        serial = str(self.config_get("serial", "") or "")
        if serial and not account.serial:
            account.serial = serial
            self._save_account(account)

        # 绑定会话自动成为推送目标（"谁绑定就推给谁"）
        if umo:
            self.subscribers.add(umo, label=account.phone_masked or umo)

        await self.hooks.emit("on_bind_success", account)
        if account.serial:
            await self.start_monitor()
            return (
                f"绑定成功（{account.phone_masked}）\n"
                f"设备：{mask_serial(account.serial)} · 监控已启动"
            )
        return (
            f"账号已绑定（{account.phone_masked}）\n"
            "还差打印机序列号：发送 /banbu sn <序列号>，或 /banbu devices 让我试试从云端列举。"
        )

    async def do_bind_serial(self, serial: str) -> str:
        account = self.load_account()
        if not account or not account.token_cipher:
            return "请先完成账号绑定：/banbu bind <手机号> → /banbu code <验证码>"
        serial = (serial or "").strip()
        if not serial:
            return "请带上序列号：/banbu sn 01P00A12345678"
        if len(serial) < 6:
            return "序列号看起来太短，请检查后重试"

        account.serial = serial
        self._save_account(account)
        self.set_config("serial", serial)
        self.event_state.reset()
        started = await self.start_monitor()
        refreshed = await self.monitor.refresh_now() if started else False
        lines = [f"设备已绑定：{mask_serial(serial)}"]
        if started:
            lines.append("监控已启动（" + str(self.config_get("monitor_mode", "auto")) + "）")
        if refreshed:
            snap = self.snapshot()
            if snap:
                lines.append(f"当前状态：{render.STATE_LABEL.get(snap.state, snap.state)}")
                if snap.percent is not None:
                    lines.append(f"进度：{snap.percent}%")
        elif started:
            lines.append("首轮数据还没回来，稍等几十秒再看 /banbu status")
        return "\n".join(lines)

    def do_unbind(self) -> str:
        # 用 JsonFile.set 清空，保证缓存与文件一致（否则 is_bound() 会继续返回 True）
        with contextlib.suppress(Exception):
            self.account_file.set({})
        self._account_cache.clear()
        self.set_config("serial", "")
        self.event_state.reset()
        self.event_state.save()
        self.subscribers.clear()
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(self.hooks.emit("on_unbind"))
        return "已清除绑定与推送目标。重新绑定发 /banbu bind <手机号>"

    async def do_list_devices(self) -> str:
        creds = self._account_for_adapter()
        if not creds.get("token"):
            return "还没绑定账号，先 /banbu bind <手机号>"
        result = await asyncio.to_thread(
            auth.list_bound_devices, self.profile, creds.get("region", "China"), creds["token"]
        )
        if not result.get("ok"):
            return (
                "云端没返回设备列表（接口可能已调整）。\n"
                f"原因：{result.get('error')}\n直接用 /banbu sn <序列号> 绑定即可。"
            )
        devices = result.get("devices") or []
        if not devices:
            return "账号下没查到设备。请确认打印机已绑定到该拓竹账号，或用 /banbu sn 手动填写。"
        lines = ["账号下的设备："]
        for item in devices:
            lines.append(f"· {item.get('name') or '未命名'} {item.get('model')} 序列号 {item.get('serial')}")
        lines.append("用 /banbu sn <序列号> 选定一台。")
        return "\n".join(lines)

    # ─────────────────────────── 命令：根 ───────────────────────────
    @filter.command_group("banbu", alias={"拓竹", "竹子"})
    async def banbu(self, event: AstrMessageEvent):
        """拓竹 A1 状态助手：绑定、查状态、设提醒。"""
        yield event.plain_result(HELP_TEXT)

    # ─────────────────────────── 命令：绑定 ───────────────────────────
    @banbu.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, phone: str = ""):
        """发送短信验证码。用法：/banbu bind 13800000000"""
        yield event.plain_result(await self.do_bind(phone))

    @banbu.command("code")
    async def cmd_code(self, event: AstrMessageEvent, code: str = ""):
        """提交验证码完成登录。用法：/banbu code 123456"""
        yield event.plain_result(await self.do_login(code, event.unified_msg_origin))

    @banbu.command("sn")
    async def cmd_sn(self, event: AstrMessageEvent, serial: str = ""):
        """绑定打印机序列号。用法：/banbu sn 01P00A12345678"""
        yield event.plain_result(await self.do_bind_serial(serial))

    @banbu.command("devices")
    async def cmd_devices(self, event: AstrMessageEvent):
        """尝试从云端列举账号下已绑定的设备。"""
        yield event.plain_result(await self.do_list_devices())

    @banbu.command("unbind")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """清除凭据。用法：/banbu unbind confirm"""
        args = self._args_after(event, "banbu", "unbind")
        if not args or args[0].lower() not in ("confirm", "yes", "确认"):
            yield event.plain_result("这会清除 token 与推送目标。确认请发送：/banbu unbind confirm")
            return
        yield event.plain_result(self.do_unbind())

    # ─────────────────────────── 命令：状态与控制 ───────────────────────────
    @banbu.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前打印机状态。"""
        yield event.plain_result(await self.render_status_text())

    async def render_status_text(self) -> str:
        if not self.is_bound():
            return "还没绑定：/banbu bind <手机号> → /banbu code <验证码> → /banbu sn <序列号>"
        age = self.snapshot_age_s()
        interval = float(self.config_get("poll_interval", 30) or 30)
        if age is None or age > interval:
            with contextlib.suppress(Exception):
                await self.monitor.refresh_now()
            age = self.snapshot_age_s()
        snap = self.snapshot()
        if not snap:
            return "还没有拿到状态数据。监控状态：\n" + self._monitor_status_text()
        return render.render_status(
            snap,
            state_age_s=int(age) if age is not None else None,
            device_name=DEVICE_NAME,
            extra_lines=[f"监控：{self.monitor_status().get('source')} · 规则 {len(self.rules.all())} 条"],
        )

    @banbu.command("monitor")
    async def cmd_monitor(self, event: AstrMessageEvent, action: str = ""):
        """后台监控开关。用法：/banbu monitor on|off|auto|status"""
        action = (action or "").strip().lower()
        if action in ("on", "start"):
            ok = await self.start_monitor()
            yield event.plain_result("监控已启动" if ok else "还没绑定完整（缺序列号或 token）")
            return
        if action in ("off", "stop"):
            await self.stop_monitor()
            yield event.plain_result("监控已停止（数据会停留在最后一次）")
            return
        if action in ("auto", "stream", "poll"):
            self.set_config("monitor_mode", action)
            if self.monitor.running:
                await self.stop_monitor()
                await self.start_monitor()
            yield event.plain_result(f"监控模式已切到 {action}")
            return
        yield event.plain_result(self._monitor_status_text())

    def _monitor_status_text(self) -> str:
        status = self.monitor_status()
        push = self.pusher.status()
        lines = [
            "监控状态：",
            f"· 运行中：{'是' if status['running'] else '否'}",
            f"· 通道：{status['source']}（模式 {status['mode']}）",
            f"· 最近数据：{'无' if status['last_data_age_s'] is None else str(status['last_data_age_s']) + ' 秒前'}",
            f"· 离线判定：{'离线' if status['offline'] else '在线'}",
            f"· 推送目标：{push['targets']} 个会话",
        ]
        if status.get("last_error"):
            lines.append(f"· 最近异常：{status['last_error']}")
        return "\n".join(lines)

    @banbu.command("light")
    async def cmd_light(self, event: AstrMessageEvent, mode: str = ""):
        """机箱灯控制。用法：/banbu light [on|off]"""
        mode = (mode or "").strip().lower()
        snap = self.snapshot()
        if not mode and snap:
            mode = "off" if (snap.light or "").lower() == "on" else "on"
        if mode not in ("on", "off"):
            yield event.plain_result("用法：/banbu light on 或 /banbu light off")
            return
        result = await self.execute_action(DeviceAction(kind="light", params={"mode": mode}))
        yield event.plain_result(("灯光已切到 " + mode) if result.ok else f"失败：{result.message}")

    @banbu.command("resume")
    async def cmd_resume(self, event: AstrMessageEvent):
        """继续打印。"""
        result = await self.execute_action(DeviceAction(kind="resume"))
        yield event.plain_result("已发送继续指令" if result.ok else f"失败：{result.message}")

    @banbu.command("pause")
    async def cmd_pause(self, event: AstrMessageEvent):
        """暂停打印（需 confirm）。"""
        args = self._args_after(event, "banbu", "pause")
        if not args or args[0].lower() not in ("confirm", "确认"):
            yield event.plain_result("暂停会打断当前打印，确认请发：/banbu pause confirm")
            return
        result = await self.execute_action(DeviceAction(kind="pause"))
        yield event.plain_result("已发送暂停指令" if result.ok else f"失败：{result.message}")

    @banbu.command("stop")
    async def cmd_stop(self, event: AstrMessageEvent):
        """停止打印（需 confirm）。"""
        args = self._args_after(event, "banbu", "stop")
        if not args or args[0].lower() not in ("confirm", "确认"):
            yield event.plain_result("停止会放弃当前任务，确认请发：/banbu stop confirm")
            return
        result = await self.execute_action(DeviceAction(kind="stop"))
        yield event.plain_result("已发送停止指令" if result.ok else f"失败：{result.message}")

    # ─────────────────────────── 命令：提醒 ───────────────────────────
    @banbu.command("remind")
    async def cmd_remind(self, event: AstrMessageEvent):
        """提醒规则。用法：/banbu remind add <类型> <值> [--llm] | list | del <id> | clear"""
        args = self._args_after(event, "banbu", "remind", "提醒")
        if not args:
            yield event.plain_result(self._remind_help())
            return
        action = args[0].lower()

        if action in ("list", "ls", "列表"):
            yield event.plain_result(render.render_rule_list(self.rules.all()))
            return
        if action in ("clear", "清空"):
            count = self.rules.clear()
            yield event.plain_result(f"已清空 {count} 条规则")
            return
        if action in ("del", "delete", "rm"):
            if len(args) < 2:
                yield event.plain_result("用法：/banbu remind del <规则id>")
                return
            ok = self.rules.delete(args[1])
            yield event.plain_result(f"已删除 {args[1]}" if ok else f"没找到规则 {args[1]}")
            return
        if action in ("add", "加", "添加"):
            rest = args[1:]
            use_llm = "--llm" in rest
            rest = [x for x in rest if x != "--llm"]
            if len(rest) < 2:
                yield event.plain_result(self._remind_help())
                return
            rtype, value = rest[0], rest[1]
            value2 = rest[2] if len(rest) > 2 else None
            num: Any = value
            num2: Any = value2
            if rtype == "temp_in_range":
                parsed = parse_temp_range(value) or (None, None)
                if not parsed[0]:
                    yield event.plain_result("温度区间写成 30-40 这种格式")
                    return
                num, num2 = parsed
            rule, error = self.rules.add(
                rtype, num, num2, channel="llm" if use_llm else None, created_by="user"
            )
            if not rule:
                yield event.plain_result(f"没加上：{error}")
                return
            yield event.plain_result(
                f"已添加 [{rule.id}] {rule.label}"
                + ("（命中时走 LLM 措辞）" if use_llm else "")
                + "\n查看：/banbu remind list"
            )
            return
        yield event.plain_result(self._remind_help())

    @staticmethod
    def _remind_help() -> str:
        types = "\n".join(f"  {key}：{meta[0]}" for key, meta in RULE_TYPES.items())
        return (
            "提醒规则用法：\n"
            "  /banbu remind add <类型> <值> [--llm]\n"
            "  /banbu remind add temp_in_range 30-40\n"
            "  /banbu remind list | del <id> | clear\n\n"
            f"可用类型：\n{types}"
        )

    @banbu.command("quiet")
    async def cmd_quiet(self, event: AstrMessageEvent, spec: str = ""):
        """安静时段。用法：/banbu quiet 23:00-07:30 或 /banbu quiet off"""
        spec = (spec or "").strip()
        if not spec:
            enabled = bool(self.config_get("quiet_hours_enabled", False))
            current = self.config_get("quiet_hours", "")
            yield event.plain_result(
                f"安静时段：{'开启' if enabled else '关闭'}（{current}）\n"
                "设置：/banbu quiet 23:00-07:30 或 /banbu quiet off"
            )
            return
        if spec.lower() in ("off", "close", "关", "关闭"):
            self.set_config("quiet_hours_enabled", False)
            yield event.plain_result("安静时段已关闭")
            return
        if not parse_quiet_range(spec):
            yield event.plain_result("格式不对，用 23:00-07:30 这种写法")
            return
        self.set_config("quiet_hours", spec)
        self.set_config("quiet_hours_enabled", True)
        yield event.plain_result(f"安静时段已设为 {spec}（期间只推 critical 事件）")

    @banbu.command("milestones")
    async def cmd_milestones(self, event: AstrMessageEvent, spec: str = ""):
        """进度里程碑。用法：/banbu milestones 25,50,75,90 或 /banbu milestones off"""
        spec = (spec or "").strip()
        current = list(self.config_get("progress_milestones", []) or [])

        if not spec or spec.lower() in ("list", "status"):
            summary = "、".join(f"{m}%" for m in current) if current else "未启用（默认不推进度）"
            yield event.plain_result(
                f"进度里程碑：{summary}\n"
                "设置：/banbu milestones 25,50,75,90 / 关闭：/banbu milestones off\n"
                "（里程碑只在你主动开启后才推；你自己用 /banbu remind 设的规则不受影响）"
            )
            return

        if spec.lower() in ("off", "none", "0", "关", "关闭"):
            self.set_config("progress_milestones", [])
            yield event.plain_result("进度里程碑已关闭，只保留你自己设的提醒规则")
            return

        marks: list[int] = []
        for token in re.split(r"[,，、\s]+", spec):
            token = token.strip().rstrip("%")
            if not token:
                continue
            try:
                value = int(float(token))
            except ValueError:
                yield event.plain_result(f"「{token}」不是数字。示例：/banbu milestones 25,50,75,90")
                return
            if not 1 <= value <= 100:
                yield event.plain_result("里程碑要落在 1–100 之间")
                return
            marks.append(value)
        if not marks:
            yield event.plain_result("没解析出数字。示例：/banbu milestones 25,50,75,90")
            return

        marks = sorted(set(marks))
        self.set_config("progress_milestones", marks)
        yield event.plain_result("进度里程碑已设为 " + "、".join(f"{m}%" for m in marks))

    @banbu.command("subscribe")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """把当前会话加入推送目标。"""
        umo = event.unified_msg_origin
        added = self.subscribers.add(umo, label=event.get_sender_name() or umo)
        yield event.plain_result("本会话已加入推送" if added else "本会话本来就在推送列表里")

    @banbu.command("unsubscribe")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """把当前会话移出推送目标。"""
        removed = self.subscribers.remove(event.unified_msg_origin)
        yield event.plain_result("已退出推送" if removed else "本会话不在推送列表里")

    # ─────────────────────────── 命令：智能 ───────────────────────────
    @banbu.command("llm")
    async def cmd_llm(self, event: AstrMessageEvent, mode: str = ""):
        """LLM 总闸。用法：/banbu llm off|summary|full"""
        alias = {"off": "off", "关": "off", "summary": "summary_only", "summary_only": "summary_only",
                 "full": "full", "全": "full"}
        mode = (mode or "").strip().lower()
        if not mode:
            yield event.plain_result(
                f"当前 LLM 档位：{self.config_get('llm_mode', 'summary_only')}\n"
                f"预算：{self.bridge.budget_status()}\n"
                "切换：/banbu llm off | summary | full"
            )
            return
        target = alias.get(mode)
        if not target:
            yield event.plain_result("只有 off / summary / full 三档")
            return
        self.set_config("llm_mode", target)
        yield event.plain_result(f"LLM 档位已切到 {target}（即时生效）")

    @banbu.command("route")
    async def cmd_route(self, event: AstrMessageEvent):
        """查看/修改 LLM 分界线。用法：/banbu route [list|set <事件> <通道>]"""
        args = self._args_after(event, "banbu", "route")
        if not args or args[0].lower() in ("list", "ls"):
            rows = router.describe(self.config_get("llm_route", {}), self.config_get("llm_mode", "summary_only"))
            lines = ["事件 → 配置 / 当前生效"]
            for event_type, configured, effective in rows:
                mark = "" if configured == effective else "  ← 被档位降级"
                lines.append(f"· {event_type}: {configured} / {effective}{mark}")
            lines.append("\n修改：/banbu route set print_finished direct")
            yield event.plain_result("\n".join(lines))
            return
        if args[0].lower() == "set":
            if len(args) < 3:
                yield event.plain_result("用法：/banbu route set <事件> <direct|llm|both>")
                return
            event_type, channel = args[1], args[2].lower()
            if channel not in ("direct", "llm", "both"):
                yield event.plain_result("通道只能是 direct / llm / both")
                return
            route = dict(self.config_get("llm_route", {}) or {})
            route[event_type] = channel
            self.set_config("llm_route", route)
            yield event.plain_result(f"{event_type} 现在走 {channel}（即时生效）")
            return
        yield event.plain_result("用法：/banbu route list 或 /banbu route set <事件> <通道>")

    @banbu.command("digest")
    async def cmd_digest(self, event: AstrMessageEvent):
        """最近打印小结（有 LLM 时由模型组织措辞）。"""
        records = self.recent_events(30)
        if not records:
            yield event.plain_result("还没有事件记录，先打印一次试试。")
            return
        counts: dict[str, int] = {}
        for record in records:
            counts[record.get("type", "?")] = counts.get(record.get("type", "?"), 0) + 1
        stats = {
            "events_total": len(records),
            "by_type": counts,
            "recent": records[:5],
        }
        text = await self.bridge.complete(
            event.unified_msg_origin,
            prompts.digest_prompt(stats),
            prompts.DIGEST_SYSTEM,
            kind="digest",
        )
        if not text:
            lines = [f"最近 {len(records)} 条事件："]
            for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"· {key}：{value} 次")
            lines.append("（LLM 未参与，纯统计）")
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result(text)

    @banbu.command("protocol")
    async def cmd_protocol(self, event: AstrMessageEvent):
        """协议档案与漂移诊断。"""
        drift = self.profile.drift_report()
        meta = self.metadata()
        lines = [
            f"版本：{meta.get('version', '未知')}（AstrBot {about.astrbot_version()}）",
            f"协议档案：{drift['profile']}",
            f"区域：{self.config_get('region', 'China')} · broker：{self.profile.mqtt_broker(self.config_get('region', 'China'))}",
            f"监控：{self.config_get('monitor_mode', 'auto')} · 轮询 {self.config_get('poll_interval', 30)}s",
        ]
        if drift["missing_fields"]:
            lines.append("注意 · 档案声明但没读到的字段：" + "、".join(
                f"{k}×{v}" for k, v in list(drift["missing_fields"].items())[:8]
            ))
        if drift["unknown_states"]:
            lines.append("注意 · 未映射的状态值：" + "、".join(drift["unknown_states"].keys()))
        if drift["unknown_fields"]:
            lines.append("提示 · 云端新增（档案未声明）字段：" + "、".join(
                list(drift["unknown_fields"].keys())[:10]
            ))
        if not self.profile.has_drift():
            lines.append("暂未发现协议漂移")
        lines.append("改字段映射请编辑 protocol_profiles/a1_v1.json 或配置 protocol_overrides")
        yield event.plain_result("\n".join(lines))

    @banbu.command("version")
    async def cmd_version(self, event: AstrMessageEvent):
        """查看插件版本与更新方式。"""
        meta = self.metadata()
        version = meta.get("version", "未知")
        repo = meta.get("repo", "") or "（metadata.yaml 未填写 repo）"
        lines = [
            f"拓竹 A1 状态助手 · {version}",
            render.RULE,
            f"插件　{meta.get('name', PLUGIN_NAME)}",
            f"仓库　{repo}",
            f"宿主　AstrBot {about.astrbot_version()}",
            "",
            "更新方式（AstrBot 内置，插件不自己轮询）：",
            "1. WebUI → 插件 → 本插件 → 更新",
            "2. 若提示“无法检测更新”，先点“更换安装源”选 Repository，填上面的仓库地址",
            "3. 手动拷贝/克隆进 data/plugins 的安装没有安装源记录，必须走第 2 步",
        ]
        yield event.plain_result("\n".join(lines))

    @banbu.command("test-event")
    async def cmd_test_event(self, event: AstrMessageEvent, event_type: str = ""):
        """造一条事件验证推送链路。用法：/banbu test-event print_finished"""
        event_type = (event_type or "print_finished").strip()
        snap = self.snapshot()
        payload: dict[str, Any] = {"task_name": "自检任务", "percent": 42, "elapsed_min": 12}
        evt = DeviceEvent(type=event_type, severity="info", data=payload, dedup_key=f"test:{time.time()}")
        await self._on_events([evt], snap)
        yield event.plain_result(
            f"已造一条 {event_type} 事件走完路由→合并→推送链路。\n"
            f"当前通道：{evt.channel}（llm_mode={self.config_get('llm_mode')}）"
        )

    @banbu.command("device")
    async def cmd_device(self, event: AstrMessageEvent, action: str = ""):
        """设备管理（烘干箱为二期）。用法：/banbu device list"""
        adapters = list_adapters()
        lines = ["已注册适配器："]
        for key, cls in adapters.items():
            mark = "可用" if getattr(cls, "available", True) else "二期"
            lines.append(f"· {key}（{cls.display_name}）{mark}")
        dryer = self.config_get("dryer", {}) or {}
        if not isinstance(dryer, dict) or not dryer.get("enabled"):
            lines.append("\n烘干箱：未启用（二期功能，配置项 dryer.enabled 打开后也仍是骨架）")
        else:
            lines.append("\n烘干箱：已启用但适配器尚未实现，调用会明确报错不崩")
        yield event.plain_result("\n".join(lines))

    # ─────────────────────────── LLM 工具 ───────────────────────────
    @filter.llm_tool(name="bambu_get_status")
    async def tool_get_status(self, event: AstrMessageEvent, detail: bool = False) -> str:
        """获取拓竹 A1 打印机当前状态（进度、层数、温度、剩余时间）。

        Args:
            detail(boolean): 是否附带 AMS、风扇、故障码等详细信息
        """
        return self.tools.get_status(detail=detail)

    @filter.llm_tool(name="bambu_get_history")
    async def tool_get_history(self, event: AstrMessageEvent, limit: int = 10) -> str:
        """获取最近的打印事件记录（完成、失败、暂停等）。

        Args:
            limit(number): 返回条数，1-50
        """
        return self.tools.get_history(limit=int(limit or 10))

    @filter.llm_tool(name="bambu_list_reminders")
    async def tool_list_reminders(self, event: AstrMessageEvent) -> str:
        """列出当前所有提醒规则和可用的规则类型。"""
        return self.tools.list_reminders()

    @filter.llm_tool(name="bambu_add_reminder")
    async def tool_add_reminder(
        self,
        event: AstrMessageEvent,
        type: str,
        value: str = "",
        value2: str = "",
        label: str = "",
        use_llm: bool = False,
    ) -> str:
        """添加一条打印机提醒规则。

        Args:
            type(string): 规则类型，如 layer_reached / percent_reached / print_remaining / bed_temp_above
            value(string): 阈值，例如层数 50、分钟 10、温度 60；temp_in_range 可写 "30-40"
            value2(string): 区间上限，仅 temp_in_range 需要
            label(string): 给用户看的规则说明，可留空
            use_llm(boolean): 命中时是否让模型润色措辞
        """
        return self.tools.add_reminder(
            type=type, value=value, value2=value2, label=label, use_llm=bool(use_llm)
        )

    @filter.llm_tool(name="bambu_delete_reminder")
    async def tool_delete_reminder(self, event: AstrMessageEvent, rule_id: str) -> str:
        """删除一条提醒规则。

        Args:
            rule_id(string): 规则 id，形如 r_ab12
        """
        return self.tools.delete_reminder(rule_id)

    @filter.llm_tool(name="bambu_set_llm_pref")
    async def tool_set_llm_pref(self, event: AstrMessageEvent, mode: str) -> str:
        """切换 LLM 使用档位（off 全模板直推 / summary_only 仅复盘问答 / full 含诊断）。

        Args:
            mode(string): off、summary_only 或 full
        """
        return self.tools.set_llm_pref(mode)

    @filter.llm_tool(name="bambu_set_route")
    async def tool_set_route(self, event: AstrMessageEvent, event_type: str, channel: str) -> str:
        """调整某类消息是否经过大模型：例如让打印失败只直推。

        Args:
            event_type(string): 事件类型，如 print_failed / print_finished / milestone
            channel(string): direct（不经过模型）、llm（模型补写后合并）、both（两条都发）
        """
        return self.tools.set_route(event_type, channel)

    @filter.llm_tool(name="bambu_control")
    async def tool_control(
        self,
        event: AstrMessageEvent,
        action: str,
        mode: str = "",
        confirm: bool = False,
    ) -> str:
        """控制打印机：开关灯、暂停、继续、停止。

        Args:
            action(string): light / pause / resume / stop
            mode(string): 仅 light 使用，on 或 off
            confirm(boolean): pause/stop 需要用户在下一轮明确确认后传 true
        """
        return await self.tools.control(action=action, mode=mode, confirm=bool(confirm))

    @filter.llm_tool(name="bambu_bind_guide")
    async def tool_bind_guide(self, event: AstrMessageEvent) -> str:
        """返回绑定步骤说明（不接收手机号、验证码等敏感参数）。"""
        return self.tools.bind_guide()

    # ─────────────────────────── 工具函数 ───────────────────────────
    def _args_after(self, event: AstrMessageEvent, *names: str) -> list[str]:
        """把命令词剥掉，返回剩余参数（兼容不同版本的参数解析）。"""
        text = (event.message_str or "").strip()
        parts = text.replace("／", "/").split()
        cleaned = [p.lstrip("/") for p in parts]
        drop = {n.lower() for n in names} | {"banbu", "拓竹", "竹子"}
        while cleaned and cleaned[0].lower() in drop:
            cleaned.pop(0)
        return cleaned
