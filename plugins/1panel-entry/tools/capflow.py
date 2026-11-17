#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两步走：先取验证码，再带验证码登录（客户端始终零 cookie，靠桥的罐子）。

用法：
    python3 capflow.py get            # 取验证码，输出 captchaID 并存图
    python3 capflow.py login <ID> <码> # 用验证码登录，再查登录态
"""
import base64
import json
import ssl
import sys
import importlib.util
import urllib.request
import urllib.error

spec = importlib.util.spec_from_file_location("lp", "/tmp/login_probe.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

BASE = "https://127.0.0.1/plugin/981760641/1panel"
KEY = open("/tmp/key.txt").read().strip()
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


mode = sys.argv[1] if len(sys.argv) > 1 else "get"

if mode == "get":
    code, raw = call("GET", "/api/v2/core/auth/captcha")
    doc = json.loads(raw)
    data = doc.get("data") or {}
    img = (data.get("imagePath") or "").split(",", 1)[-1]
    open("/tmp/cap_bridge.png", "wb").write(base64.b64decode(img))
    print("captchaID=%s  len=%d" % (data.get("captchaID"), len(img)))
else:
    cid, answer = sys.argv[2], sys.argv[3]
    payload = {
        "name": "yueke",
        "password": lp.encrypt_password("yyk5213344", KEY),
        "captcha": answer,
        "captchaID": cid,
        "authMethod": "session",
        "language": "zh",
    }
    code, raw = call("POST", "/api/v2/core/auth/login", payload,
                     {"EntranceCode": base64.b64encode(b"index").decode()})
    print("登录 ->", code, raw.decode("utf-8", "ignore")[:200])
    for p, m in [("/api/v2/core/auth/current", "GET"),
                 ("/api/v2/core/settings/search/base", "POST")]:
        code, raw = call(m, p, None if m == "GET" else {})
        print("  ", p, "->", code, raw.decode("utf-8", "ignore")[:180])
