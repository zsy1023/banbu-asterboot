"""云端认证层的回归测试：发码分支、错误码翻译、uid 获取。

全部用假 session，不联网。重点覆盖这次线上踩到的分支：
未安装 curl_cffi + HTTP 504 → 必须明确告诉用户"短信大概率没下发"。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bambu_a1 import auth  # noqa: E402
from bambu_a1.compat import load_profile  # noqa: E402


class _Resp:
    def __init__(self, status: int = 200, text: str = "", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Cookies:
    def __init__(self, token: str = "csrf-token"):
        self._token = token

    def get(self, name: str):
        return self._token if name == "bbl_csrf_token" else None


class _Session:
    def __init__(self, post_resp: _Resp, csrf_resp: _Resp | None = None):
        self.cookies = _Cookies()
        self._post_resp = post_resp
        self._get_resp = csrf_resp or _Resp(200, "")
        self.closed = False
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        return self._get_resp

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._post_resp

    def close(self):
        self.closed = True


class TestSendSms(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def _run(self, session, *, curl_ok: bool):
        with mock.patch.object(auth, "_session", lambda: session), \
                mock.patch.object(auth, "_CURL_OK", curl_ok):
            return auth.send_sms(self.profile, "China", "17700005191")

    def test_success_has_no_warning(self):
        session = _Session(_Resp(200, '{"success":true}'))
        result = self._run(session, curl_ok=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["warning"], "")
        self.assertTrue(session.closed)

    def test_posts_to_sms_url_with_csrf_header(self):
        session = _Session(_Resp(200, "{}"))
        self._run(session, curl_ok=True)
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertTrue(url.endswith("/api/v1/user-service/user/sendsmscode"))
        self.assertEqual(kwargs["json"], {"phone": "17700005191", "type": "codeLogin"})
        self.assertEqual(kwargs["headers"]["x-bbl-csrf-token"], "csrf-token")
        self.assertEqual(kwargs["timeout"], auth.SMS_TIMEOUT)

    def test_504_without_curl_cffi_warns_sms_not_delivered(self):
        """线上那次：504 + 没装 curl_cffi → 短信其实没下发，必须说清楚。"""
        session = _Session(_Resp(504, "<html>cloudflare</html>"))
        result = self._run(session, curl_ok=False)
        self.assertTrue(result["ok"], "流程仍继续，让用户能重试 code")
        self.assertEqual(result["status"], 504)
        self.assertIn("curl_cffi", result["warning"])
        self.assertIn("pip install", result["warning"])

    def test_504_with_curl_cffi_says_wait_a_bit(self):
        session = _Session(_Resp(504, ""))
        result = self._run(session, curl_ok=True)
        self.assertEqual(result["status"], 504)
        self.assertIn("通常仍会到达", result["warning"])
        self.assertNotIn("pip install", result["warning"])

    def test_network_exception_still_returns_ok(self):
        class Boom(_Session):
            def post(self, url, **kwargs):
                raise ConnectionResetError("boom")

        result = self._run(Boom(_Resp(200)), curl_ok=True)
        self.assertTrue(result["ok"], "请求已发出只是响应异常，按原项目语义继续")
        self.assertEqual(result["status"], 200)

    def test_rejects_masked_phone(self):
        result = self._run(_Session(_Resp(200)), curl_ok=True)
        self.assertTrue(result["ok"])
        bad = auth.send_sms(self.profile, "China", "177****5191")
        self.assertFalse(bad["ok"])
        self.assertIn("脱敏", bad["error"])

    def test_rejects_empty_phone(self):
        bad = auth.send_sms(self.profile, "China", "   ")
        self.assertFalse(bad["ok"])
        self.assertIn("为空", bad["error"])


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def _run(self, resp):
        with mock.patch.object(auth, "_session", lambda: _Session(resp)):
            return auth.login_with_code(self.profile, "China", "17700005191", "123456")

    def test_success_returns_token(self):
        result = self._run(_Resp(200, "{}", {"accessToken": "TOKEN-XYZ"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["token"], "TOKEN-XYZ")

    def test_code_expired(self):
        result = self._run(_Resp(400, "", {"code": 1}))
        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])

    def test_code_wrong(self):
        result = self._run(_Resp(400, "", {"code": 2}))
        self.assertFalse(result["ok"])
        self.assertIn("不正确", result["error"])

    def test_missing_token_is_reported(self):
        result = self._run(_Resp(200, "{}", {}))
        self.assertFalse(result["ok"])
        self.assertIn("accessToken", result["error"])

    def test_network_error_is_readable(self):
        class Boom(_Session):
            def post(self, url, **kwargs):
                raise ConnectionError("dns fail")

        with mock.patch.object(auth, "_session", lambda: Boom(_Resp(200))):
            result = auth.login_with_code(self.profile, "China", "17700005191", "1")
        self.assertFalse(result["ok"])
        self.assertIn("无法连接", result["error"])


class TestFetchUid(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("a1_v1")

    def test_returns_mqtt_username(self):
        resp = _Resp(200, "{}", {"uid": 123, "name": "me"})
        with mock.patch.object(auth.requests, "get", return_value=resp):
            result = auth.fetch_uid(self.profile, "China", "TOKEN")
        self.assertTrue(result["ok"])
        self.assertEqual(result["uid"], "123")
        self.assertEqual(result["username"], "u_123")

    def test_missing_uid_is_reported(self):
        resp = _Resp(200, "{}", {"uid": None})
        with mock.patch.object(auth.requests, "get", return_value=resp):
            result = auth.fetch_uid(self.profile, "China", "TOKEN")
        self.assertFalse(result["ok"])
        self.assertIn("uid", result["error"])


class TestRegionEndpoints(unittest.TestCase):
    def test_china_and_global_use_different_api_hosts(self):
        profile = load_profile("a1_v1")
        cn = profile.endpoints("China")
        glob = profile.endpoints("Global")
        self.assertIn("api.bambulab.cn", cn["login_url"])
        self.assertIn("api.bambulab.com", glob["login_url"])
        # 收码域名两区共用（与原项目一致）
        self.assertEqual(cn["sms_url"], glob["sms_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
