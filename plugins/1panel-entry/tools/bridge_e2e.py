#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：模拟“完全不保存 cookie 的客户端”，看桥接层能否维持登录态。

关键点：本脚本用 urllib 但**不挂 CookieJar**，即浏览器侧一个 cookie 都不带。
如果桥接层工作正常，登录后 auth/current 依然应该返回 200。
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
        raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    return code, raw


def show(label, code, raw):
    text = raw.decode("utf-8", "ignore")
    print("%-46s %s %s" % (label, code, text[:150]))


# 0) 桥接健康
code, raw = call("GET", "/health")
show("health（看罐子里有哪些 cookie）", code, raw)

# 1) 未登录时
code, raw = call("GET", "/api/v2/core/auth/current")
show("登录前 auth/current", code, raw)

# 2) 登录（客户端不带任何 cookie）
payload = {
    "name": "yueke",
    "password": lp.encrypt_password("yyk5213344", KEY),
    "authMethod": "session",
    "language": "zh",
}
code, raw = call("POST", "/api/v2/core/auth/login", payload,
                 {"EntranceCode": base64.b64encode(b"index").decode()})
show("登录（浏览器零 cookie）", code, raw)

# 3) 登录后，仍然零 cookie
for p in ["/api/v2/core/auth/current", "/api/v2/core/settings/search/base"]:
    code, raw = call("GET" if "current" in p else "POST", p, None if "current" in p else {})
    show("登录后 " + p, code, raw)

# 4) 罐子里现在应该有几个 cookie
code, raw = call("GET", "/health")
show("health（登录后）", code, raw)
