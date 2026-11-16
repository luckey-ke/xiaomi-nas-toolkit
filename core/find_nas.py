#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_nas.py — 在局域网里自动寻找小米智能存储（RP05）

原理：
  1. 小米存储 App 登录后会在本机留下客户端证书（minasCert 目录），
     证书里藏着这台设备的 SNI 前缀（形如 nas.<uid>.<设备指纹>）。
  2. 扫描局域网内所有开放了 443 端口的主机，逐个用这个 SNI 前缀
     发起 TLS 握手——只有真正的小米 NAS 才能完成握手并亮出匹配的设备证书。
  3. 整个过程是只读探测，不登录、不改任何东西。

用法：
  python find_nas.py                     # 用默认证书目录扫描
  python find_nas.py --cert-dir DIR      # 指定证书目录
  python find_nas.py --subnet 192.168.1  # 只扫指定网段（调试用）

输出：
  找到 → 打印一行 JSON：{"ok": true, "host": "<NAS的IP>", "uid": "<UID>", "sni": "..."}
  没找到 → 退出码 1，不输出 JSON（调用方据此降级为手动输入）
"""

import argparse
import base64
import concurrent.futures as cf
import glob
import ipaddress
import json
import os
import re
import socket
import ssl
import sys

# 扫描参数：443 是 NAS 的身份认证端口；0.4s 超时对局域网足够
SCAN_PORT = 443
CONNECT_TIMEOUT = 0.4
TLS_TIMEOUT = 3.0
WORKERS = 64


def find_cert_files(cert_dir):
    """在证书目录里找齐三件套：客户端证书、私钥、CA 链。"""
    certs = sorted(glob_path(cert_dir, "*_cert.pem"))
    if not certs:
        return None
    cert = certs[0]
    base = os.path.basename(cert)[:-len("_cert.pem")]
    key = os.path.join(cert_dir, base + "_private_key.pem")
    ca = os.path.join(cert_dir, "ca_chain.pem")
    if not (os.path.exists(key) and os.path.exists(ca)):
        return None
    return cert, key, ca


def glob_path(directory, pattern):
    return glob.glob(os.path.join(directory, pattern))


def sni_prefix_from_cert(cert_path):
    """
    从客户端证书里提取 SNI 前缀。
    证书主题里包含形如 nas.<uid>.<40位十六进制> 的字符串，
    它同时是设备 TLS 服务认可的 SNI 前缀。
    """
    with open(cert_path, "rb") as f:
        pem = f.read()
    der = base64.b64decode(b"".join(pem.splitlines()[1:-1]))
    m = re.search(rb"nas\.[0-9]+\.[0-9a-f]+", der)
    return m.group(0).decode() if m else None


def relax_strict_x509(ctx):
    """
    Python 3.13+ 的 OpenSSL 默认开启 X509_STRICT，
    而厂商 CA 证书的 basicConstraints 未标 critical，会被直接拒绝。
    这里只撤掉严格位，CA 链/有效期/主机名校验全部保留。
    """
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT


def local_subnets():
    """
    枚举本机所有 IPv4 网段（按 /24 处理）。
    用"向外部地址发起 UDP 连接"的技巧拿到出口网卡地址——不实际发包。
    """
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不真的发包，只用于选路
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    nets = []
    for ip in sorted(addrs):
        if ip.startswith("127."):
            continue
        nets.append(ipaddress.ip_network(ip + "/24", strict=False))
    # 去重（多网卡可能在同一网段）
    return list({str(n): n for n in nets}.values())


def tcp_open(ip):
    """快速探测 443 端口是否开放。"""
    try:
        with socket.create_connection((str(ip), SCAN_PORT), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def verify_device(ip, cert, key, ca, sni):
    """
    对开放了 443 的主机做身份确认：
    用客户端证书 + 设备 SNI 前缀完成握手，并校验对端证书确实属于这台 NAS。
    """
    ctx = ssl.create_default_context(cafile=ca)
    relax_strict_x509(ctx)
    ctx.load_cert_chain(cert, key)
    try:
        raw = socket.create_connection((str(ip), SCAN_PORT), timeout=TLS_TIMEOUT)
        with ctx.wrap_socket(raw, server_hostname=sni + ".0") as s:
            peer_der = s.getpeercert(binary_form=True) or b""
            # 对端证书里应包含同一个 nas.<uid>.<指纹> 标识
            if sni.encode() in peer_der:
                return True
    except (ssl.SSLError, OSError):
        pass
    return False


def scan(cert_dir, only_subnet=None):
    files = find_cert_files(cert_dir)
    if not files:
        return {"ok": False, "reason": "cert_not_found"}
    cert, key, ca = files
    sni = sni_prefix_from_cert(cert)
    if not sni:
        return {"ok": False, "reason": "sni_parse_failed"}

    if only_subnet:
        nets = [ipaddress.ip_network(only_subnet + ".0/24", strict=False)]
    else:
        nets = local_subnets()
    if not nets:
        return {"ok": False, "reason": "no_network"}

    # 第一阶段：并发探测哪些主机开了 443
    hosts = [ip for net in nets for ip in net.hosts()]
    open_hosts = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for ip, is_open in zip(hosts, pool.map(tcp_open, hosts)):
            if is_open:
                open_hosts.append(ip)

    # 第二阶段：对开放的主机做身份确认（通常只剩个位数，很快）
    for ip in open_hosts:
        if verify_device(ip, cert, key, ca, sni):
            uid = sni.split(".")[1]
            return {"ok": True, "host": str(ip), "uid": uid, "sni": sni + ".0"}
    return {"ok": False, "reason": "nas_not_found", "scanned": len(hosts)}


def main():
    ap = argparse.ArgumentParser(description="扫描局域网寻找小米智能存储")
    ap.add_argument("--cert-dir", default=os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "minasCert"))
    ap.add_argument("--subnet", default="", help="只扫指定网段，如 192.168.1")
    args = ap.parse_args()

    result = scan(args.cert_dir, args.subnet or None)
    if result["ok"]:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    # 失败原因输出到 stderr，stdout 保持干净，方便脚本判断
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
