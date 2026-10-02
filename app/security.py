#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全工具模块

- Web 登录密码：使用 PBKDF2-HMAC-SHA256 加盐哈希（单向，不可还原）
- Steam 账号密码：使用 Fernet 对称加密（可逆，需解密后传给 steamcmd）
- 会话 token：随机生成
"""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

# ----------------------------------------------------------------------------
# Web 登录密码：PBKDF2 加盐哈希
# 存储格式: pbkdf2_sha256$<salt_hex>$<hash_hex>
# ----------------------------------------------------------------------------
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256$" + salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        _, salt_hex, hash_hex = stored.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except Exception:  # noqa: BLE001
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk, expected)


# ----------------------------------------------------------------------------
# Steam 账号密码：Fernet 对称加密（可逆）
# ----------------------------------------------------------------------------
def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def encrypt_secret(plaintext: str, key: str) -> str:
    """加密明文，返回可安全存入配置的可打印字符串。"""
    if not plaintext:
        return ""
    f = Fernet(key.encode("utf-8"))
    token = f.encrypt(plaintext.encode("utf-8"))
    return "fernet$" + token.decode("utf-8")


def decrypt_secret(token: str, key: str) -> str:
    """解密；若未加密或密钥不匹配返回空串。"""
    if not token or not token.startswith("fernet$"):
        return token or ""
    try:
        f = Fernet(key.encode("utf-8"))
        return f.decrypt(token[len("fernet$"):].encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------------------
# 会话 token
# ----------------------------------------------------------------------------
def new_session_token() -> str:
    return secrets.token_urlsafe(32)
