#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：不同登录参数组合下服务端返回的报文与大小，用来和 nginx 日志里的 124 字节对上号。"""
import json
import ssl
import base64
import http.cookiejar
import urllib.request

BASE = "https://127.0.0.1/plugin/981760641/1panel"
USER = "yueke"
PWD = "yyk5213344"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    ), jar


# ---- 复刻前端加密 ----
def encrypt_password(password, pem):
    import subprocess
    rsa_out = subprocess.run(
        ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", "/tmp/pub.pem",
         "-pkeyopt", "rsa_padding_mode:pkcs1"],
        input=b"", capture_output=True)
    return rsa_out


def do(label, body, headers=None, use_jar=True):
    op, jar = opener()
    if use_jar:
        try:
            op.open(BASE + "/api/v2/core/auth/captcha", timeout=10).read()
        except Exception:
            pass
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(BASE + "/api/v2/core/auth/login",
                                 data=json.dumps(body).encode(), headers=hdr, method="POST")
    try:
        r = op.open(req, timeout=15)
        raw = r.read()
        code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    print("%-34s HTTP %s  %3d bytes  %s" % (label, code, len(raw), raw.decode("utf-8", "ignore")))
    return raw


# 取公钥
op, jar = opener()
op.open(BASE + "/api/v2/core/auth/captcha", timeout=10).read()
key_cookie = [c for c in jar if c.name == "panel_public_key"]
print("panel_public_key cookie:", "YES" if key_cookie else "NO")
pub = key_cookie[0].value if key_cookie else ""
import re
m = re.search(r"(-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----)",
              pub.replace("\\n", "\n"), re.S)
if not m:
    # 也许 cookie 里是 base64 之后又 URL 编码
    try:
        dec = urllib.parse.unquote(pub)
        m = re.search(r"(-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----)",
                      dec.replace("\\n", "\n"), re.S)
    except Exception:
        pass
print("公钥解析:", "OK" if m else "FAILED", (pub[:48] + "...") if pub else "")

if m:
    open("/tmp/pub.pem", "w").write(m.group(1))
    # 前端算法：32 hex AES key，RSA 加密 key，AES-256-CBC 加密密码
    import subprocess
    aes = subprocess.run(["openssl", "rand", "-hex", "16"], capture_output=True).stdout.decode().strip()
    open("/tmp/aes.key", "wb").write(bytes.fromhex(aes))
    iv = subprocess.run(["openssl", "rand", "-hex", "16"], capture_output=True).stdout.decode().strip()
    ct = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-K", aes, "-iv", iv,
                         "-in", "/dev/stdin"], input=PWD.encode(), capture_output=True).stdout
    rsa = subprocess.run(["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", "/tmp/pub.pem",
                          "-pkeyopt", "rsa_padding_mode:pkcs1"], input=aes.encode(),
                         capture_output=True).stdout
    enc = "%s:%s:%s" % (base64.b64encode(rsa).decode(), iv,
                        base64.b64encode(ct).decode())
    print("加密串长度:", len(enc))

    do("A 加密+EntranceCode(无验证码)",
       {"name": USER, "password": enc, "authMethod": "session", "language": "zh"},
       {"EntranceCode": base64.b64encode(b"index").decode()})

    do("B 加密+EntranceCode+假验证码",
       {"name": USER, "password": enc, "captcha": "1", "captchaID": "FAKE",
        "authMethod": "session", "language": "zh"},
       {"EntranceCode": base64.b64encode(b"index").decode()})

    do("C 加密+无EntranceCode",
       {"name": USER, "password": enc, "authMethod": "session", "language": "zh"})

    do("D 明文+EntranceCode",
       {"name": USER, "password": PWD, "authMethod": "session", "language": "zh"},
       {"EntranceCode": base64.b64encode(b"index").decode()})
