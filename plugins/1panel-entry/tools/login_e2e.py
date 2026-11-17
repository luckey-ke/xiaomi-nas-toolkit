"""端到端验证：登录 → 带 cookie 调当前用户接口。"""
import base64
import http.cookiejar
import json
import ssl
import urllib.request
import importlib.util

spec = importlib.util.spec_from_file_location('lp', '/tmp/login_probe.py')
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

key = open('/tmp/key.txt').read().strip()
base = 'https://127.0.0.1/plugin/981760641/1panel'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(jar)
)

payload = {
    'name': 'yueke',
    'password': lp.encrypt_password('yyk5213344', key),
    'authMethod': 'session',
    'language': 'zh',
}
req = urllib.request.Request(
    base + '/api/v2/core/auth/login',
    data=json.dumps(payload).encode(),
    headers={
        'Content-Type': 'application/json',
        'EntranceCode': base64.b64encode(b'index').decode(),
    },
    method='POST',
)
r = op.open(req, timeout=15)
print('登录:', r.read()[:200].decode())
print('cookies:', [(c.name, c.path) for c in jar])

r2 = op.open(base + '/api/v2/core/auth/current', timeout=15)
body = r2.read().decode()
print('当前用户:', r2.status, body[:220])
