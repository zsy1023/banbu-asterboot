"""拓竹云端认证：发码 / 验证码登录 / token 校验 / 换 uid。

全部端点来自协议档案（compat.Profile.endpoints），不硬编码域名。
踩坑点（来自 banbu-a1-Status-acquisition 的实践）：
  - token 不跨区，收码区域与登录区域必须一致
  - 发码接口经常超时/504，但短信其实已下发 → 按"已请求"处理
  - web 域有双提交 CSRF：先 GET /api/csrf 拿 bbl_csrf_token，POST 时回显头
  - 裸 requests 易被 Cloudflare 拦，装了 curl_cffi 就用它伪装指纹
"""

from __future__ import annotations

from typing import Any

import requests

from .compat import Profile
from .credentials import mask_phone
from .logging_compat import get_logger

logger = get_logger("auth")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SMS_TIMEOUT = 75
IMPERSONATE = "chrome"

try:  # 可选依赖：装了才伪装指纹
    from curl_cffi import requests as _curl_requests

    _CURL_OK = True
except ImportError:  # pragma: no cover - 取决于运行环境
    _curl_requests = None
    _CURL_OK = False


def has_curl_cffi() -> bool:
    return _CURL_OK


def _session():
    if _CURL_OK:
        return _curl_requests.Session(impersonate=IMPERSONATE)
    return requests.Session()


def _kw() -> dict[str, Any]:
    return {"impersonate": IMPERSONATE, "verify": False} if _CURL_OK else {"verify": False}


def _fetch_csrf(session, csrf_url: str) -> str | None:
    """拓竹 web 域的双提交 CSRF：cookie 自动带上，另需回显请求头。"""
    try:
        resp = session.get(
            csrf_url,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 CSRF token 失败: %s: %s", type(exc).__name__, str(exc)[:150])
        return None
    try:
        token = session.cookies.get("bbl_csrf_token")
    except Exception:  # noqa: BLE001
        token = None
    if not token:
        logger.warning("CSRF 端点未返回 bbl_csrf_token（HTTP %s）", resp.status_code)
    return token


def send_sms(profile: Profile, region: str, phone: str) -> dict[str, Any]:
    """发送短信验证码。

    返回 {"ok": bool, "status": int, "error": str}；
    发出去就算成功——这个接口经常 504，但短信通常已下发。
    """
    phone = (phone or "").strip()
    if not phone:
        return {"ok": False, "status": 0, "error": "手机号为空"}
    if "*" in phone or not phone.isdigit():
        return {"ok": False, "status": 0, "error": "手机号格式不正确（请输入完整 11 位号码，不要用脱敏号）"}

    urls = profile.endpoints(region)
    masked = mask_phone(phone)
    logger.info("请求短信验证码: %s", masked)

    try:
        session = _session()
        csrf = _fetch_csrf(session, urls["csrf_url"])
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        }
        if csrf:
            headers["x-bbl-csrf-token"] = csrf
        resp = session.post(
            urls["sms_url"],
            json={"phone": phone, "type": "codeLogin"},
            headers=headers,
            timeout=SMS_TIMEOUT,
            **_kw(),
        )
        logger.info("验证码请求已发出: %s | HTTP %s", masked, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "验证码请求已发出（响应异常）: %s | %s: %s",
            masked,
            type(exc).__name__,
            str(exc)[:150],
        )
    return {"ok": True, "status": 200, "error": ""}


def login_with_code(
    profile: Profile, region: str, phone: str, code: str
) -> dict[str, Any]:
    """验证码登录，返回 accessToken。

    错误码（上游约定）：1 = 验证码过期/不存在，2 = 验证码不正确。
    """
    urls = profile.endpoints(region)
    masked = mask_phone(phone)
    try:
        session = _session()
        resp = session.post(
            urls["login_url"],
            json={"account": phone, "code": code},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": BROWSER_UA,
            },
            timeout=25,
            **_kw(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("登录请求异常: %s: %s", type(exc).__name__, str(exc)[:150])
        return {"ok": False, "error": f"无法连接拓竹云服务（{type(exc).__name__}）"}

    logger.info("登录接口响应(%s): HTTP %s", masked, resp.status_code)

    if resp.status_code != 200:
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        error_code = data.get("code")
        if error_code == 1:
            return {"ok": False, "error": "验证码已过期或不存在，请重新发送验证码"}
        if error_code == 2:
            return {"ok": False, "error": "验证码不正确，请重新输入"}
        return {
            "ok": False,
            "error": str(data.get("error") or (resp.text or "登录失败")[:160]),
        }

    try:
        token = (resp.json() or {}).get("accessToken")
    except ValueError:
        token = None
    if not token:
        return {"ok": False, "error": "登录成功但未返回 accessToken（协议可能已变更）"}
    return {"ok": True, "token": token, "error": ""}


def fetch_uid(profile: Profile, region: str, token: str) -> dict[str, Any]:
    """用 token 换 uid，进而得到 MQTT 用户名 u_{uid}。"""
    urls = profile.endpoints(region)
    try:
        resp = requests.get(
            urls["preference_url"],
            headers={"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA},
            timeout=15,
            verify=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"获取账号信息失败（{type(exc).__name__}）"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"获取账号信息失败（HTTP {resp.status_code}）"}
    try:
        uid = (resp.json() or {}).get("uid")
    except ValueError:
        uid = None
    if not uid:
        return {"ok": False, "error": "账号信息里没有 uid（协议可能已变更）"}
    return {"ok": True, "uid": str(uid), "username": f"u_{uid}", "error": ""}


def test_token(profile: Profile, region: str, token: str) -> bool:
    urls = profile.endpoints(region)
    try:
        resp = requests.get(
            urls["preference_url"],
            headers={"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA},
            timeout=10,
            verify=False,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def list_bound_devices(profile: Profile, region: str, token: str) -> dict[str, Any]:
    """列举账号下已绑定的设备（失败不阻塞主流程，改为提示手动 /banbu sn）。"""
    urls = profile.endpoints(region)
    try:
        resp = requests.get(
            urls["bind_list_url"],
            headers={"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA},
            timeout=15,
            verify=False,
        )
        if resp.status_code != 200:
            return {"ok": False, "devices": [], "error": f"HTTP {resp.status_code}"}
        data = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "devices": [], "error": type(exc).__name__}

    devices = []
    for item in data.get("devices") or []:
        if not isinstance(item, dict):
            continue
        devices.append({
            "serial": item.get("dev_id") or item.get("serial") or "",
            "name": item.get("name") or "",
            "model": item.get("dev_model_name") or item.get("model") or "",
        })
    return {"ok": True, "devices": devices, "error": ""}
