#!/usr/bin/env python3
"""1Panel market entry — static UI server.

Serves src/ui on 127.0.0.1:$PLUG_PORT (proxied by the NAS frontend) and
exposes /health so the page can tell whether the real panel is up.
Deliberately tiny: no auth here, 1Panel has its own login on :18080.
"""

import json
import os
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PLUG_PORT", "9304"))
# 面板页由 nginx 从 www 软链静态托管，这里额外监听 0.0.0.0，
# 仅用于浏览器侧 /health 探测（纯静态 + 端口探测，无敏感数据）。
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "18080"))

_HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.abspath(os.path.join(_HERE, "..", "ui"))


def panel_up() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.7)
    try:
        sock.connect(("127.0.0.1", PANEL_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def _json(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({"up": panel_up(), "panel_port": PANEL_PORT})
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
