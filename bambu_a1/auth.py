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
    """发送短信验证码（流程对齐 banbu-a1-Status-acquisition/auth.py 的 send_sms）。

    照搬 ha-bambulab / bambuddy 的做法：
      - 端点 {sms_base}/api/v1/user-service/user/sendsmscode
      - 先 GET /api/csrf 取 bbl_csrf_token，同一 session 带 cookie + x-bbl-csrf-token 头
      - 用 curl_cffi impersonate="chrome" 过 Cloudflare（**这一步是能否收到短信的关键**）

    返回 {"ok", "status", "error", "warning"}：
      - 发出去就算流程成功（该接口经常 504，但短信通常已下发）
      - 但如果 5xx 且没装 curl_cffi，短信基本发不出去 → 用 warning 明确告诉用户
    """
    phone = (phone or "").strip()
    if not phone:
        logger.warning("发送验证码失败：手机号为空")
        return {"ok": False, "status": 0, "error": "手机号为空"}
    if "*" in phone or not phone.isdigit():
        # 常见误用：把界面上的脱敏号（177****5191）直接发过来
        logger.warning("发送验证码失败：手机号格式非法 %r", phone)
        return {
            "ok": False,
            "status": 0,
            "error": "手机号格式不正确（请输入完整 11 位号码，不要用脱敏号）",
        }

    urls = profile.endpoints(region)
    masked = mask_phone(phone)
    logger.info("正在请求短信验证码: %s -> %s", masked, urls["sms_url"])
    if not _CURL_OK:
        logger.warning(
            "未安装 curl_cffi：拓竹云端有 Cloudflare，裸 requests 常返回 504 且短信不下发。"
            "请在 AstrBot 的 Python 环境里 pip install curl_cffi 后重试。"
        )

    status = 0
    body_snippet = ""
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
        status = int(getattr(resp, "status_code", 0) or 0)
        body_snippet = str(getattr(resp, "text", "") or "")[:200]
        logger.info("验证码请求已发出: %s | HTTP %s | 响应 %r", masked, status, body_snippet)
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        # 请求已发出，只是响应超时/异常；短信有时已经下发
        logger.info(
            "验证码请求已发出（响应异常）: %s | %s: %s",
            masked,
            type(exc).__name__,
            str(exc)[:150],
        )

    warning = ""
    if status >= 500:
        if not _CURL_OK:
            warning = (
                "云端返回 5xx（Cloudflare 拦截），且当前未安装 curl_cffi，短信大概率没有下发。\n"
                "请先在 AstrBot 的 Python 环境安装：pip install curl_cffi，然后重新 /banbu bind"
                "（或直接重载插件让它按 requirements.txt 自动安装）。"
            )
        else:
            warning = "云端返回 5xx（上游超时），短信通常仍会到达。等 1 分钟没收到再重发一次。"

    return {"ok": True, "status": status or 200, "error": "", "warning": warning}


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
