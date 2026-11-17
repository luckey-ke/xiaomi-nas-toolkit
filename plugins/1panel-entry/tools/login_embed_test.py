"""模拟客户端：不依赖 cookie 里的公钥（用 JS 里嵌入的那份）做登录。"""
import base64
import json
import ssl
import sys
import urllib.request

sys.path.insert(0, '/tmp')
import importlib.util

spec = importlib.util.spec_from_file_location('lp', '/tmp/login_probe.py')
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

key = open('/tmp/key.txt').read().strip()
print('用嵌入公钥（%d 字节）加密密码' % len(key))

base = 'https://127.0.0.1/plugin/981760641/1panel'
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

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
try:
    r = urllib.request.urlopen(req, timeout=15, context=ctx)
    print('登录 ->', r.status, r.read()[:300].decode())
    print('Set-Cookie:', r.headers.get_all('Set-Cookie'))
except urllib.error.HTTPError as e:
    print('登录 ->', e.code, e.read()[:300].decode())
