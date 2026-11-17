#!/usr/bin/env python3
"""1Panel market entry — UI server + same-origin API bridge.

为什么需要 API 桥（重要，别再删）：
    应用市场客户端的 webview 完全不保存 cookie：
      · 客户端 profile 的 Cookies 库里 0 行（实测）
      · 页面里 document.cookie 长度为 0（实测）
    而 1Panel 的登录态（psession）、安全入口、公钥全都靠 cookie 传递。
    所以浏览器侧永远拿不到会话，登录成功也会立刻 401「当前会话已过期」。

    这里做一个 **服务端 cookie 罐**：浏览器只跟本进程说话，本进程带着自己
    维护的 cookie 去访问 127.0.0.1:18080。用户登录一次后，后续所有 API
    请求由本进程补上 Cookie，浏览器不需要任何 cookie 能力。

路由：
    /health            → 面板存活探测
    /api/...           → 反代到面板，注入 cookie 罐里的 Cookie
    其他                → 静态文件（src/ui，即镜像后的 1Panel 前端）
"""

import http.client
import json
import os
import re
import select
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PLUG_PORT", "9304"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PANEL_HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "18080"))
PANEL_TIMEOUT = int(os.environ.get("PANEL_TIMEOUT", "300"))

_HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.abspath(os.path.join(_HERE, "..", "ui"))

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
STRIP_REQ = HOP_BY_HOP | {"host", "cookie", "accept-encoding", "content-length"}
STRIP_RESP = HOP_BY_HOP | {"content-length", "set-cookie", "content-encoding"}

# Set-Cookie 里 Expires 带逗号，只在“逗号后紧跟 name=”处切分
_COOKIE_SPLIT = re.compile(r",(?=\s*[A-Za-z0-9._\-]+=)")

_jars = {}  # key -> {cookie_name: value}
_jar_lock = threading.Lock()

# 诊断日志：PANEL_DEBUG=1 时把每次 API 请求/响应写一行到 /tmp/1panel/bridge.log
DEBUG = os.environ.get("PANEL_DEBUG", "0") == "1"
DEBUG_LOG = os.path.join("/tmp", "1panel", "bridge.log")
_log_lock = threading.Lock()


def jar_key(handler):
    """会话罐的键。

    早期版本想按 NAS 登录令牌（X-Nas-Auth）分罐，做到“每个 NAS 用户一份 1Panel
    会话”。但客户端 webview 发起的 XHR 是否稳定带同一个 Authorization 无法在
    设备侧验证，令牌一旦变化就会出现“刚登录又被踢”的现象，可靠性优先，这里
    统一用一个共享罐。

    安全边界：1Panel 本体本来就监听 0.0.0.0:18080，局域网内可直接访问，
    共享罐不额外扩大暴露面。真正的防线是“不要把 NAS 暴露到公网”。
    """
    return "shared"



def dlog(text):
    if not DEBUG:
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        with _log_lock:
            if os.path.exists(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > 4 * 1024 * 1024:
                os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
            with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))
    except OSError:
        pass


def redact(body):
    """别把密码原文写进日志。"""
    if not body:
        return ""
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    except Exception:
        return "<binary>"
    text = re.sub(r'("password"\s*:\s*")[^"]*"', r'\1<redacted>"', text)
    return text[:500]


def encrypt_password(password, pub_cookie):
    """按 1Panel v2.5 的规则加密密码：

        n   = 16 字节随机数的 hex（32 字符，作为 AES-256 密钥）
        rsa = RSA_PKCS1v15(n, panel_public_key)
        iv  = 16 字节随机
        aes = AES-256-CBC(password, key=n, iv)
        password 字段 = rsa:iv:aes（都 base64）
    """
    import base64
    import os as _os
    import urllib.parse

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pem = base64.b64decode(urllib.parse.unquote(pub_cookie))
    pub = load_pem_public_key(pem)
    aes_key = _os.urandom(16).hex()
    rsa = pub.encrypt(aes_key.encode(), asym_padding.PKCS1v15())
    iv = _os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key.encode()), modes.CBC(iv)).encryptor()
    cipher_text = enc.update(data) + enc.finalize()
    b64 = lambda b: base64.b64encode(b).decode()
    return "%s:%s:%s" % (b64(rsa), b64(iv), b64(cipher_text))


def maybe_encrypt_login(key, path, body):
    """登录请求的密码兜底。

    面板要求密码必须是「rsa:iv:aes」三段式。前端靠读 cookie 里的公钥来加密，
    而客户端不存 cookie —— 万一前端加密没生效（发了明文），这里用罐子里最新的
    公钥重新加密，保证登录不会因为“密码对但格式不对”而失败。
    """
    if not body or "/core/auth/login" not in path:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body
    pwd = payload.get("password")
    if not isinstance(pwd, str) or pwd.count(":") >= 2:
        return body
    pub_cookie = jar_get(key, "panel_public_key")
    if not pub_cookie:
        dlog("login: 罐子里没有公钥，跳过兜底加密")
        return body
    try:
        payload["password"] = encrypt_password(pwd, pub_cookie)
        dlog("login: 明文密码已在服务端重新加密")
    except Exception as exc:
        dlog("login: 兜底加密失败 %s" % exc)
        return body
    return json.dumps(payload).encode("utf-8")



def jar_header(key):
    with _jar_lock:
        return "; ".join("%s=%s" % (k, v) for k, v in _jars.get(key, {}).items())


def jar_get(key, name):
    with _jar_lock:
        return _jars.get(key, {}).get(name)


def jar_names(key):
    with _jar_lock:
        return sorted(_jars.get(key, {}).keys())


def jar_store(key, msg):
    """把面板下发的 Set-Cookie 收进罐子（面板重启后会话失效，罐子会自然过期）。"""
    for raw in msg.get_all("Set-Cookie") or []:
        for one in _COOKIE_SPLIT.split(raw):
            parts = [p.strip() for p in one.split(";") if p.strip()]
            if not parts or "=" not in parts[0]:
                continue
            name, value = parts[0].split("=", 1)
            name, value = name.strip(), value.strip()
            dead = value == "" or value == "deleted"
            for attr in parts[1:]:
                a = attr.lower().replace(" ", "")
                if a.startswith("max-age=") and a.split("=", 1)[1] in ("0", "-1"):
                    dead = True
            with _jar_lock:
                bag = _jars.setdefault(key, {})
                if dead:
                    bag.pop(name, None)
                else:
                    bag[name] = value


def panel_up() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.7)
    try:
        sock.connect((PANEL_HOST, PANEL_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "1panel-entry"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    # ---------- 小工具 ----------
    def _json(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

    # ---------- 路由 ----------
    def _dispatch(self, method):
        path = self.path.split("?")[0]
        if path.startswith("/health"):
            key = jar_key(self)
            self._json({"up": panel_up(), "panel_port": PANEL_PORT,
                        "jar": key, "cookies": jar_names(key)})
            return
        if path.startswith("/api/") or path == "/api":
            self._bridge(method)
            return
        if method == "GET" and path in ("/", ""):
            self.path = "/index.html"
        getattr(SimpleHTTPRequestHandler, "do_" + method)(self)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    # ---------- API 桥 ----------
    def _bridge(self, method):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._ws_tunnel()
            return

        length = self.headers.get("Content-Length")
        body = None
        if length:
            try:
                body = self.rfile.read(int(length))
            except (ValueError, OSError):
                body = None

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in STRIP_REQ:
                continue
            headers[key] = value
        key = jar_key(self)
        if body:
            body = maybe_encrypt_login(key, self.path, body)
        cookie = jar_header(key)
        if cookie:
            headers["Cookie"] = cookie
        # 1Panel 对非 GET 请求校验 CSRF：要求 X-CSRF-Token 与 cookie pcsrftoken 一致。
        # 浏览器读不到 cookie（客户端不存 cookie），所以由桥补上这个头。
        if method.upper() not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            if not any(k.lower() == "x-csrf-token" for k in headers):
                token = jar_get(key, "pcsrftoken")
                if token:
                    headers["X-CSRF-Token"] = token
        headers["Host"] = "%s:%d" % (PANEL_HOST, PANEL_PORT)
        headers["X-Real-IP"] = "127.0.0.1"
        headers["X-Forwarded-For"] = "127.0.0.1"
        headers["X-Forwarded-Proto"] = "https"

        conn = http.client.HTTPConnection(PANEL_HOST, PANEL_PORT, timeout=PANEL_TIMEOUT)
        try:
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # 面板没起来 / 超时
            try:
                conn.close()
            except Exception:
                pass
            dlog("%s %s -> 502 %s" % (method, self.path, exc))
            self._json({"code": 502, "message": "面板未响应: %s" % exc, "data": None}, 502)
            return

        _log_req = "%s %s req=%s" % (method, self.path, redact(body))
        try:
            jar_store(key, resp.msg)
            self.send_response(resp.status, resp.reason or None)
            for hname, hvalue in resp.getheaders():
                if hname.lower() in STRIP_RESP:
                    continue
                self.send_header(hname, hvalue)
            if method == "HEAD" or resp.status in (204, 304):
                if resp.status != 304:
                    self.send_header("Content-Length", "0")
                self.end_headers()
                return
            declared = resp.getheader("Content-Length")
            if declared is not None and int(declared) <= 4096:
                data = resp.read()
                dlog("%s -> %s resp=%s" % (_log_req, resp.status, redact(data)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif declared is not None:
                dlog("%s -> %s (%s bytes, streamed)" % (_log_req, resp.status, declared))
                self.send_header("Content-Length", declared)
                self.end_headers()
                remain = int(declared)
                while remain > 0:
                    chunk = resp.read(min(65536, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
            else:
                data = resp.read()
                dlog("%s -> %s resp=%s" % (_log_req, resp.status, redact(data)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- WebSocket 隧道 ----------
    def _ws_tunnel(self):
        """终端、容器日志走 WebSocket。这里做一次裸 socket 升级 + 双向搬运，
        因为浏览器同样拿不到 cookie，必须在服务端补 Cookie 才能通过面板鉴权。
        """
        cookie = jar_header(jar_key(self))
        try:
            sock = socket.create_connection((PANEL_HOST, PANEL_PORT), timeout=15)
        except OSError as exc:
            self._json({"code": 502, "message": "面板未响应: %s" % exc, "data": None}, 502)
            return

        lines = ["%s %s HTTP/1.1" % (self.command, self.path),
                 "Host: %s:%d" % (PANEL_HOST, PANEL_PORT)]
        for name, value in self.headers.items():
            if name.lower() in ("host", "cookie", "connection"):
                continue
            lines.append("%s: %s" % (name, value))
        lines.append("Connection: Upgrade")
        if cookie:
            lines.append("Cookie: %s" % cookie)
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
        except OSError:
            sock.close()
            self.close_connection = True
            return

        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        if b"\r\n\r\n" not in buf:
            sock.close()
            self._json({"code": 502, "message": "面板未完成 WebSocket 握手", "data": None}, 502)
            return

        head, _, rest = buf.partition(b"\r\n\r\n")
        dlog("WS %s -> %s" % (self.path, head.split(b"\r\n", 1)[0].decode("latin-1", "replace")))
        try:
            self.wfile.write(head + b"\r\n\r\n")
            if rest:
                self.wfile.write(rest)
            self.wfile.flush()
            self.connection.settimeout(None)
            sock.settimeout(None)
            while True:
                ready, _, _ = select.select([sock, self.connection], [], [], 900)
                if not ready:
                    break
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (self.connection if src is sock else sock).sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            self.close_connection = True
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()