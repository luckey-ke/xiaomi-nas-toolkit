#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证桥的兜底：故意发**明文**密码，看服务端重新加密后能否登录成功。

用法：python3 plainlogin.py <captchaID> <验证码>
"""
import base64
import json
import ssl
import sys
import urllib.request
import urllib.error

BASE = "https://127.0.0.1/plugin/981760641/1panel"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(method, path, body=None, headers=None):
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30, context=ctx)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


cid, answer = sys.argv[1], sys.argv[2]
payload = {
    "name": "yueke",
    "password": "yyk5213344",          # 故意明文
    "captcha": answer,
    "captchaID": cid,
    "authMethod": "session",
    "language": "zh",
}
code, raw = call("POST", "/api/v2/core/auth/login", payload,
                 {"EntranceCode": base64.b64encode(b"index").decode()})
print("明文登录 ->", code, raw.decode("utf-8", "ignore")[:200])
code, raw = call("GET", "/api/v2/core/auth/current")
print("  auth/current ->", code, raw.decode("utf-8", "ignore")[:120])
