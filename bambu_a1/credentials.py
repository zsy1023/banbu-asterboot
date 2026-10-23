"""凭据加密存储：AES-256-GCM + 机器绑定盐派生密钥。

安全模型（ARCHITECTURE.md 5.2 / 13 节）：
  key = scrypt(master_secret, SHA256(hostname + MAC + instance_id))
  ct  = AES-256-GCM(key, nonce, aad=b"bambu:/account")
落盘只有密文 + key_id；换机器或改 secret_key 会因 key_id 不匹配而明确报错，
不会静默丢数据。未安装 cryptography 时**拒绝绑定**，绝不降级明文。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import uuid
from typing import Any

from .models import now

AAD = b"bambu:/account"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


class CryptoUnavailable(RuntimeError):
    pass


class DecryptError(RuntimeError):
    pass


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise CryptoUnavailable(
            "缺少 cryptography 库，无法加密保存凭据。请先安装：pip install cryptography"
        ) from exc
    return AESGCM


def machine_fingerprint(instance_id: str = "") -> bytes:
    """机器绑定盐：主机名 + MAC + 实例 ID，换机即失效。"""
    host = socket.gethostname()
    try:
        mac = f"{uuid.getnode():012x}"
    except Exception:  # noqa: BLE001 - 极端环境下拿不到 MAC 也不能崩
        mac = "unknown"
    raw = f"{host}|{mac}|{instance_id}".encode("utf-8")
    return hashlib.sha256(raw).digest()


def derive_key(master_secret: str, salt: bytes) -> bytes:
    """优先 scrypt；OpenSSL 不支持时退回 PBKDF2-HMAC-SHA256。"""
    password = (master_secret or "").encode("utf-8")
    try:
        return hashlib.scrypt(
            password, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
        )
    except (ValueError, OSError):  # pragma: no cover - 依赖 OpenSSL 编译选项
        return hashlib.pbkdf2_hmac("sha256", password, salt, 200_000, dklen=32)


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def generate_secret(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("ascii")


class CredentialBox:
    """对一组敏感字段做加解密。"""

    def __init__(self, master_secret: str, instance_id: str = ""):
        _aesgcm()  # 早失败：没有 cryptography 直接在这里报可读错误
        if not master_secret:
            raise ValueError("master_secret 不能为空")
        self._key = derive_key(master_secret, machine_fingerprint(instance_id))
        self.key_id = key_id(self._key)

    def encrypt(self, plaintext: str) -> dict[str, Any]:
        if plaintext is None:
            return {}
        nonce = os.urandom(12)
        ct = _aesgcm()(self._key).encrypt(nonce, plaintext.encode("utf-8"), AAD)
        return {
            "v": 1,
            "key_id": self.key_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
            "at": now(),
        }

    def decrypt(self, blob: dict[str, Any]) -> str:
        if not blob:
            return ""
        stored_key_id = blob.get("key_id")
        if stored_key_id and stored_key_id != self.key_id:
            raise DecryptError(
                "凭据无法解密：密钥已变化（换机器或改过 secret_key），请重新 /banbu bind"
            )
        try:
            nonce = base64.b64decode(blob["nonce"])
            ct = base64.b64decode(blob["ct"])
            return _aesgcm()(self._key).decrypt(nonce, ct, AAD).decode("utf-8")
        except DecryptError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DecryptError(f"凭据解密失败：{type(exc).__name__}") from exc


def mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) >= 7:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone or "-"


def mask_serial(serial: str) -> str:
    serial = str(serial or "")
    if len(serial) > 6:
        return f"{serial[:4]}***{serial[-2:]}"
    return serial or "-"
