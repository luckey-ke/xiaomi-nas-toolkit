#!/usr/bin/env python3
"""Local plugin catalog guard for Xiaomi NAS (v4).

plugincenter judges any plugin missing from the cloud server.list as
"taken down" and uninstalls it. This guard sits between plugincenter and the
cloud: it forwards the catalog request, merges our self-developed plugins into
the response, and fails closed (HTTP 503) when the cloud is unreachable so a
failed update can never trigger deletion.

Listens on 127.0.0.1 only. The custom entries file holds server-list shaped
records: {"<plugin>": {"info": ..., "resource": ...}}.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("CATALOG_GUARD_PORT", "9310"))
UPSTREAM = os.environ.get(
    "CATALOG_UPSTREAM",
    "https://device.eco.mi.com/ota-server/version/plugin/get",
)
ENTRIES_FILE = os.environ.get(
    "CATALOG_ENTRIES",
    "/data/minas/plugins/catalog/entries.json",
)
UPSTREAM_TIMEOUT = float(os.environ.get("CATALOG_UPSTREAM_TIMEOUT", "15"))


def load_entries():
    try:
        with open(ENTRIES_FILE, encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            return {
                key: value for key, value in data.items()
                if isinstance(value, dict) and "info" in value and "resource" in value
            }
    except (OSError, ValueError):
        pass
    return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep plugincenter request bodies and identifiers out of the journal.
        return

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            self._reply(200, {"ok": True, "service": "catalog-guard", "port": PORT})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"ok": False, "error": "bad content-length"})
            return
        if length <= 0 or length > 65536:
            self._reply(400, {"ok": False, "error": "bad request size"})
            return
        body = self.rfile.read(length)
        request = urllib.request.Request(
            UPSTREAM,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "plugincenter-guard/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                if response.status != 200:
                    self._reply(502, {"ok": False, "error": "upstream status %d" % response.status})
                    return
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._reply(502, {"ok": False, "error": "upstream status %d" % exc.code})
            return
        except Exception as exc:
            # Fail closed: plugincenter aborts the update instead of deleting.
            self._reply(503, {"ok": False, "error": "upstream unavailable: %s" % type(exc).__name__})
            return
        try:
            catalog = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            self._reply(502, {"ok": False, "error": "upstream returned invalid json"})
            return
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), dict):
            self._reply(502, {"ok": False, "error": "upstream catalog shape changed"})
            return
        data = catalog["data"]
        merged = 0
        for plugin, entry in load_entries().items():
            if plugin not in data:
                data[plugin] = entry
                merged += 1
        catalog["guardMerged"] = merged
        self._reply(200, catalog)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[catalog-guard] listening on %s:%d entries=%s" % (HOST, PORT, ENTRIES_FILE), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
