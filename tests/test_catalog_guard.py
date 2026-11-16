#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catalog_guard.py 离线测试。不连接 NAS，上游用本地假服务模拟。

覆盖四个场景，都是真实故障路径：
  1. 正常合并：云端条目保留，自研插件被追加，形状不合法的条目丢弃
  2. 上游返回非法 JSON   → 502（拒绝合并）
  3. 上游返回 5xx        → 502
  4. 上游完全不可达      → 503（fail-closed：宁可不同步，也不删插件）

用法：python -X utf8 tests/test_catalog_guard.py
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = ROOT / "core" / "catalog_guard.py"


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        if Upstream.mode == "down":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if Upstream.mode == "garbage":
            body = b"not-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if Upstream.mode == "shape":
            body = json.dumps({"code": 0, "data": "unexpected-string"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({
            "code": 0,
            "message": "",
            "data": {
                "central": {"info": {"plugin": "central"}, "resource": {"mpk": "https://example/central"}},
                "dockerctl": {"info": {"plugin": "dockerctl"}, "resource": {"mpk": "https://example/official"}},
            },
            "result": "ok",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, body=b"{}"):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/ota-server/version/plugin/get" % port,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def start_guard(guard_port, upstream_url, entries_path):
    env = dict(
        os.environ,
        CATALOG_GUARD_PORT=str(guard_port),
        CATALOG_UPSTREAM=upstream_url,
        CATALOG_ENTRIES=str(entries_path),
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(GUARD)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % guard_port, timeout=1):
                return proc
        except Exception:
            if time.monotonic() > deadline:
                print(proc.communicate(timeout=1)[0])
                raise RuntimeError("守卫未能启动")
            time.sleep(0.1)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = "http://127.0.0.1:%d/ota-server/version/plugin/get" % upstream_port

    guard_port = free_port()
    with tempfile.TemporaryDirectory() as temp:
        entries = pathlib.Path(temp) / "entries.json"
        entries.write_text(json.dumps({
            "dockerctl": {"info": {"plugin": "dockerctl"}, "resource": {"mpk": "/data/INFO"}},
            "sysmon": {"info": {"plugin": "sysmon"}, "resource": {"mpk": "/data/INFO"}},
            "invalid": {"bad": "shape"},
        }), encoding="utf-8")

        guard = start_guard(guard_port, upstream_url, entries)
        try:
            # 1. 正常合并
            status, reply = request(guard_port)
            assert status == 200, (status, reply)
            assert reply["code"] == 0, reply
            assert "central" in reply["data"], reply
            # 云端已有的条目不应被覆盖
            assert reply["data"]["dockerctl"]["resource"]["mpk"] == "https://example/official", reply
            # 云端没有的应被追加
            assert "sysmon" in reply["data"], reply
            # 形状不合法的应被丢弃
            assert "invalid" not in reply["data"], reply
            assert reply["guardMerged"] == 1, reply
            print("[1/4] 合并：cloud 保留 + 自研追加 + 非法条目丢弃  OK")

            # 2. 上游返回非法 JSON
            Upstream.mode = "garbage"
            status, reply = request(guard_port)
            assert status == 502, (status, reply)
            print("[2/4] 上游非法 JSON → 502 拒绝合并  OK")

            # 3. 上游 5xx
            Upstream.mode = "down"
            status, reply = request(guard_port)
            assert status == 502, (status, reply)
            print("[3/4] 上游 5xx → 502  OK")

            # 4. 上游形状变化
            Upstream.mode = "shape"
            status, reply = request(guard_port)
            assert status == 502, (status, reply)
            print("[4/4] 上游形状变化 → 502 拒绝合并  OK")
        finally:
            stop(guard)
            upstream.shutdown()

    # 5. 上游完全不可达：换一个没人监听的端口
    dead_port = free_port()
    guard_port2 = free_port()
    with tempfile.TemporaryDirectory() as temp:
        entries = pathlib.Path(temp) / "entries.json"
        entries.write_text(json.dumps({"sysmon": {"info": {}, "resource": {}}}), encoding="utf-8")
        guard = start_guard(guard_port2, "http://127.0.0.1:%d/x" % dead_port, entries)
        try:
            status, reply = request(guard_port2)
            assert status == 503, (status, reply)
            print("[5/5] 上游不可达 → 503 fail-closed（不删插件）  OK")
        finally:
            stop(guard)

    print("\nCATALOG GUARD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
