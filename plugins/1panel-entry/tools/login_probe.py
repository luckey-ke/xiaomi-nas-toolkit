#!/usr/bin/env python3
"""按 1Panel v2.5 前端的加密方案构造登录请求，用于验证后端是否强制要求加密密码。

前端逻辑（useGlobalStore-*.js）：
  n   = 16 字节随机数的 hex 字符串（32 字符，作为 AES-256 密钥）
  rsa = RSA_PKCS1v15(n, panel_public_key) -> base64
  iv  = 16 字节随机
  aes = AES-256-CBC(password, key=n, iv) -> base64
  password 字段 = rsa:iv:aes
"""
import base64
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as apad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key
import http.cookiejar


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def encrypt_password(password: str, pub_key_b64: str) -> str:
    pem = base64.b64decode(urllib.parse.unquote(pub_key_b64))
    key = load_pem_public_key(pem)
    n = os.urandom(16).hex()
    rsa = key.encrypt(n.encode(), apad.PKCS1v15())
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(n.encode("utf-8")), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return f"{b64(rsa)}:{b64(iv)}:{b64(ct)}"


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1/plugin/981760641/1panel"
    user = sys.argv[2] if len(sys.argv) > 2 else "yueke"
    pwd = sys.argv[3] if len(sys.argv) > 3 else "yyk5213344"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )

    # 1) 取验证码接口，顺便拿到 panel_public_key cookie
    r = op.open(base + "/api/v2/core/auth/captcha", timeout=15)
    body = json.loads(r.read().decode())
    print("captcha ->", r.status, "captchaID =", (body.get("data") or {}).get("captchaID"))

    key = None
    for c in jar:
        if c.name == "panel_public_key":
            key = c.value
    print("public_key 长度 =", len(key or ""))
    if not key:
        print("没有拿到公钥 cookie，无法加密")
        return

    payload = {
        "name": user,
        "password": encrypt_password(pwd, key),
        "authMethod": "session",
        "language": "zh",
    }
    req = urllib.request.Request(
        base + "/api/v2/core/auth/login",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "EntranceCode": base64.b64encode(b"index").decode(),
        },
        method="POST",
    )
    try:
        r = op.open(req, timeout=15)
        print("login ->", r.status, r.read()[:300].decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        print("login ->", e.code, e.read()[:300].decode("utf-8", "ignore"))
    print("cookies:", [(c.name, c.value[:20]) for c in jar])


if __name__ == "__main__":
    main()
