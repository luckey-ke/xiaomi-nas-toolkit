#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 WSL 环境下的运行入口（Windows 主力场景）。

上游工具通过 `wsl bash -c '... nc ...'` 把脚本送进 NAS 的 socat root shell
（端口 2323）。很多 Windows 机器没有启用 WSL，或 WSL 被组策略禁用，此时
上游链路直接失败。本文件用 Python 标准库 socket 直连 2323，发送完全相同的
字节流，不调用 wsl.exe / nc，也不修改任何系统设置。

上游源码本身不做任何改动——补丁在运行时以 monkey patch 方式注入：
  · SocatClient.run_command  Windows 分支改为纯 socket
  · _cleanup_via_shell       上游在 Windows 下没有可用通道，失败路径无法清理，
                             这里补上（否则临时 SMB 账号会残留）

用法（在仓库根目录执行）：
  python -X utf8 core/nowsl_run.py selftest
  python -X utf8 core/nowsl_run.py probe --passive    完全被动，不认证、不写设备
  python -X utf8 core/nowsl_run.py probe              加 mTLS 只读查询，仍不写设备
  python -X utf8 core/nowsl_run.py root               执行 root（需 config 已填）
  python -X utf8 core/nowsl_run.py deploy --list
  python -X utf8 core/nowsl_run.py deploy --select dockerctl,sysmon

配置来自 config/config.json；上游代码来自 vendor/upstream（由 bootstrap 拉取）。
"""
import base64
import importlib.util
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"
VENDOR = ROOT / "vendor" / "upstream"


def die(message, code=2):
    print("错误：%s" % message, file=sys.stderr)
    raise SystemExit(code)


def load_config():
    if not CONFIG_PATH.is_file():
        die("找不到 %s，请先复制 config/config.example.json 并填写" % CONFIG_PATH)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        die("配置不是合法 JSON：%s" % exc)


def find_root_tool():
    """在上游仓库里定位 root 工具目录（版本号会随固件变化）。"""
    matches = sorted(VENDOR.glob("xiaomi-nas-root-tool-*/xiaomi-nas-root.py"))
    if not matches:
        die("vendor/upstream 里找不到 xiaomi-nas-root-tool，请先运行 windows/bootstrap.ps1")
    return matches[-1]


def find_deploy_tool():
    path = VENDOR / "deploy-plugins.py"
    if not path.is_file():
        die("vendor/upstream 里找不到 deploy-plugins.py，请先运行 windows/bootstrap.ps1")
    return path


def tcp_open(host, port, timeout=3):
    """探测 TCP 端口是否可连通（用于判断 2323 root shell 是否在线）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_shell(host, port, payload, timeout=60):
    """把字节流送进 socat root shell，收集全部文本输出。"""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(1.0)
    try:
        sock.sendall(data)
        chunks = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                buf = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not buf:
                break
            chunks.append(buf)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        sock.close()


def build_feed(b64, tmp="/tmp/dp.b64"):
    """把 base64 脚本包装成 PTY 可执行的字节流；长脚本分块下发。"""
    if len(b64) <= 200:
        return "echo %s | base64 -d | bash\nexit\n" % b64
    chunks = [b64[i:i + 56] for i in range(0, len(b64), 56)]
    lines = ["sleep 0.4", "rm -f %s" % tmp]
    lines += ["echo -n %s >> %s" % (chunk, tmp) for chunk in chunks]
    lines.append("base64 -d %s > /tmp/dp.sh; bash /tmp/dp.sh; rm -f %s /tmp/dp.sh" % (tmp, tmp))
    lines.append("exit")
    return "\n".join(lines) + "\n"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch_deploy(mod):
    """替换 SocatClient.run_command 的 Windows 分支。"""
    original = mod.SocatClient.run_command

    def run_command(self, script, timeout=60):
        if os.name != "nt":
            return original(self, script, timeout)
        script = script.replace("\r\n", "\n")
        b64 = base64.b64encode(script.encode()).decode()
        feed = build_feed(b64)
        output = tcp_shell(self.connection.host, self.connection.shell_port, feed, timeout)
        return self._strip_noise(output)

    mod.SocatClient.run_command = run_command


def patch_root(mod):
    """替换 root 工具的自动清理通道（失败路径也必须能清理）。"""
    def cleanup(args, disarm):
        try:
            if mod.tcp_open(args.ip, args.shell_port, timeout=3):
                b64 = base64.b64encode(disarm.encode()).decode()
                feed = build_feed(b64, tmp="/tmp/cl.b64")
                output = tcp_shell(args.ip, args.shell_port, feed, timeout=30)
                if "CLEAN_OK" in output:
                    mod.log("ok", "清理完成（CLEAN_OK：各步骤全部成功）")
                    return
                if "SMB_CLEAN_SKIP" in output:
                    mod.log("warn", "SMB 配置已变化或不再属于本次运行；保留账号和共享，跳过删除")
            mod._dump_manual_cleanup(args, disarm)
        except Exception as exc:
            mod.log("warn", "清理流程异常（%s），请手动执行" % type(exc).__name__)
            mod._dump_manual_cleanup(args, disarm)

    mod._cleanup_via_shell = cleanup


def relax_strict_x509():
    """放宽 X.509 严格模式，兼容新版 Python/OpenSSL。

    Python 3.13 起的 OpenSSL 3.5 默认开启 X509_STRICT，而厂商 CA 证书的
    basicConstraints 扩展没有标记为 critical，于是任何用 CA 链校验的握手都会
    报 "Basic Constraints of CA cert not marked critical"——这不是中间人攻击
    信号，只是厂商证书不合规。

    这里只撤掉"严格位"：CA 链校验、有效期、主机名（SAN）校验全部保留。
    副作用仅限本进程。
    """
    flag = getattr(ssl, "VERIFY_X509_STRICT", None)
    if flag is None:
        return False
    original = ssl.create_default_context

    def patched(*args, **kwargs):
        ctx = original(*args, **kwargs)
        try:
            ctx.verify_flags = ctx.verify_flags & ~flag
        except Exception:
            pass
        return ctx

    ssl.create_default_context = patched
    return True


def cert_paths(cfg):
    access = cfg.get("access") or {}
    cert_dir = access.get("cert_dir") or os.path.expandvars(r"%LOCALAPPDATA%\minasCert")
    return Path(cert_dir)


def probe(passive_only=False, host=None):
    """只读预检：本机前置条件与利用链前半段在当前固件上是否仍然成立。

    默认会用客户端证书做 mTLS 认证，但只调用查询类接口——不创建账号、
    不建目录、不写 /etc、不启动服务。--passive 则完全不认证。

    注意：MKCOL 换行目录名与 uci heredoc 注入点属于写操作，本预检刻意不测，
    因此「通过」不等于「漏洞一定还在」。
    """
    cfg = load_config()
    mod = load_module("minas_root_probe", find_root_tool())
    host = host or (cfg.get("device") or {}).get("host")
    if not host:
        die("配置里没有 device.host")
    uid_cfg = str((cfg.get("device") or {}).get("uid") or "")
    print("设备地址:", host, "| 配置 UID:", uid_cfg or "(未填，将自动解析)")

    ok = True
    print("\n[1] 本机证书")
    cert_dir = cert_paths(cfg)
    certs = sorted(Path(cert_dir).glob("*_cert.pem")) if Path(cert_dir).is_dir() else []
    if not certs:
        print("  FAIL 找不到 *_cert.pem，目录:", cert_dir)
        print("       请先安装「小米智能存储」桌面端并登录目标设备")
        return 1
    cert = certs[0]
    key = cert.with_name(cert.name.replace("_cert.pem", "_private_key.pem"))
    ca = Path(cert_dir) / "ca_chain.pem"
    print("  cert:", cert.name)
    print("  private key:", key.exists(), "| CA chain:", ca.exists())
    if not key.exists() or not ca.exists():
        print("  FAIL 缺少私钥或 CA 链")
        return 1
    try:
        info = ssl._ssl._test_decode_cert(str(cert))
        print("  subject:", info.get("subject"))
        print("  valid:", info.get("notBefore"), "->", info.get("notAfter"))
    except Exception as exc:
        print("  WARN 无法解析证书有效期:", exc)
    try:
        ctx = ssl.create_default_context(cafile=str(ca))
        ctx.load_cert_chain(str(cert), str(key))
        print("  OK  Python/OpenSSL 可加载客户端证书")
    except Exception as exc:
        ok = False
        print("  FAIL Python 无法加载客户端证书，root 工具会直接失败:", exc)

    print("\n[2] 网络与设备身份（被动，不认证）")
    for port in (22, 443, 445, 5000, 2323):
        state = "OPEN" if mod.tcp_open(host, port, timeout=2) else "closed"
        print("  %-5s %s" % (port, state))
    detected_sn = None
    try:
        raw = socket.create_connection((host, 443), timeout=5)
        pctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        pctx.check_hostname = False
        pctx.verify_mode = ssl.CERT_NONE
        with pctx.wrap_socket(raw, server_hostname="probe.local") as sock:
            der = sock.getpeercert(binary_form=True) or b""
        match = re.search(rb"nas\.[0-9]+\.[0-9a-f]+", der)
        detected_sn = match.group(0).decode() if match else None
        print("  服务端证书 CN 前缀:", detected_sn or "(未解析)")
    except Exception as exc:
        print("  WARN 被动读取 443 证书失败:", exc)

    if passive_only:
        print("\n--passive：已跳过所有认证请求，未使用客户端证书连接设备。")
        return 0 if ok else 1

    print("\n[3] mTLS 只读查询（不写入设备）")
    try:
        nas = mod.Nas(host, str(cert_dir))
        print("  OK  SNI 探测成功:", nas.sn, "| 解析 UID:", nas.uid)
        if uid_cfg and uid_cfg != str(nas.uid):
            ok = False
            print("  FAIL 配置 UID 与证书不一致（配置 %s，设备 %s）" % (uid_cfg, nas.uid))
        elif not uid_cfg:
            print("  提示：配置里 UID 为空，可填入 %s" % nas.uid)
    except Exception as exc:
        print("  FAIL 证书会话/SNI 探测失败，root 工具前半段已不适用:", exc)
        return 1
    try:
        body = nas.json_body(nas.https("/cgi-bin/luci/filemgr/get_pool_info",
                                      {"selector": ["webDAV"]}))
        webdav = (body.get("data") or {}).get("webDAV") or {}
        print("  OK  get_pool_info 可用 | WebDAV 用户:", webdav.get("username"),
              "| 端口:", webdav.get("port"), "| 密码: <不显示>")
        if not webdav.get("username"):
            ok = False
            print("  FAIL 未返回 WebDAV 凭据")
    except Exception as exc:
        ok = False
        print("  FAIL get_pool_info 调用失败:", exc)
    try:
        accounts = mod._smb_accounts(nas)
        print("  OK  get_samba_account 可用 | 现有 SMB 账号数:", len(accounts))
        for sid, entry in sorted(accounts.items()):
            print("      id=%s username=%s status=%s" %
                  (sid, entry.get("username"), entry.get("status")))
        if any(str(entry.get("username", "")).lower() == "u" + str(nas.uid)
               for entry in accounts.values()):
            print("  注意: 已存在同名 u<uid> 账号，工具会主动停止以保护原配置")
    except Exception as exc:
        ok = False
        print("  FAIL SMB 账号查询失败:", exc)

    print("\n结论:", "前半段链路在当前固件上仍成立" if ok else "前置条件或接口已变化，不建议继续")
    print("注意: 本预检不测试写操作（MKCOL 换行目录名、uci heredoc 注入），")
    print("      因此通过 ≠ 漏洞一定还在；失败则基本可判定工具不适用。")
    return 0 if ok else 1


def selftest():
    """本地回环自检：不连接 NAS，验证 socket 通道与两处补丁。"""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    seen = []

    def serve():
        for _ in range(4):
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                while True:
                    buf = conn.recv(65536)
                    if not buf:
                        break
                    seen.append(buf)
            except (socket.timeout, OSError):
                pass
            try:
                conn.sendall(b"root@nas:~# \r\nCLEAN_OK\r\n")
            except OSError:
                pass
            conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    out = tcp_shell("127.0.0.1", port, "echo hello\nexit\n", timeout=5)
    assert "CLEAN_OK" in out, "socket 通道失败"
    assert any(b"echo hello" in buf for buf in seen), "载荷未送达"
    print("[1/3] 原始 socket 通道：OK")

    short = build_feed(base64.b64encode(b"echo hi").decode())
    assert short.endswith("exit\n"), "短载荷格式错误"
    long_b64 = base64.b64encode(b"echo hi\n" * 100).decode()
    long_feed = build_feed(long_b64)
    assert "echo -n" in long_feed and "/tmp/dp.sh" in long_feed, "长载荷格式错误"
    print("[2/3] 短/长载荷构造：OK")

    deploy = load_module("minas_deploy", find_deploy_tool())
    patch_deploy(deploy)

    class Conn:
        host = "127.0.0.1"
        shell_port = port

    client = object.__new__(deploy.SocatClient)
    client.connection = Conn()
    out2 = client.run_command("echo deploy-probe", timeout=5)
    assert "CLEAN_OK" in out2, "补丁后的 run_command 失败"
    print("[3/3] 补丁后的 deploy run_command：OK")

    root = load_module("minas_root", find_root_tool())
    patch_root(root)

    class Args:
        ip = "127.0.0.1"
        shell_port = port

    root._cleanup_via_shell(Args(), "echo CLEAN_OK")
    print("[附加] 补丁后的 root 清理：OK（上游在 Windows 下此路径不可用）")
    print("自检通过——全程未连接任何 NAS")
    return 0


def run_script(script, env=None, timeout=180):
    """在 2323 root shell 上执行一段脚本，返回输出。

    env 里的变量以 `K='V' sh -c '...'` 前缀注入，避免出现在进程命令行历史里。
    """
    cfg = load_config()
    device = cfg.get("device") or {}
    host = device.get("host")
    if not host:
        die("配置里没有 device.host")
    port = int(device.get("shell_port") or 2323)
    script = script.replace("\r\n", "\n")
    if env:
        prefix = " ".join("%s=%s" % (key, shlex_quote(str(value))) for key, value in env.items())
        script = "%s sh -c %s\n" % (prefix, shlex_quote(script))
    b64 = base64.b64encode(script.encode()).decode()
    feed = build_feed(b64, tmp="/tmp/nx.b64")
    return tcp_shell(host, port, feed, timeout)


def shlex_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def write_deploy_config(cfg, channel):
    """把统一配置翻译成上游 deploy-plugins.py 认识的格式。

    transport 是 SSH 的运行环境（auto/native/wsl），不能填 socat——
    socat 是 channel（执行通道）的取值，两者是不同维度。
    transport 固定 native：本项目就是给「没有/不用 WSL」的 Windows 准备的，
    auto 在装有 wsl.exe 的机器上（哪怕 WSL 功能没启用）会误选 wsl 直接炸。
    """
    device = cfg.get("device") or {}
    access = cfg.get("access") or {}
    target = VENDOR / "deploy-config.json"
    payload = {
        "host": device.get("host") or "",
        "uid": str(device.get("uid") or ""),
        "ssh_key": access.get("key_path") or "",
        "user": access.get("ssh_user") or "root",
        "port": int(access.get("ssh_port") or 22),
        "transport": "native",
        "channel": channel,
        "shell_port": int(device.get("shell_port") or 2323),
        "cert_dir": access.get("cert_dir") or "",
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    kind, rest = sys.argv[1], sys.argv[2:]
    if kind in ("probe", "info", "root", "deploy", "exec"):
        if relax_strict_x509() and kind in ("probe", "info"):
            print("提示: 已放宽 X.509 严格校验（厂商 CA 的 basicConstraints 未标 critical）；"
                  "CA 链与主机名校验仍然有效", file=sys.stderr)
    if kind == "selftest":
        return selftest()
    if kind == "probe":
        host = None
        if "--host" in rest:
            idx = rest.index("--host")
            if idx + 1 < len(rest):
                host = rest[idx + 1]
        return probe(passive_only="--passive" in rest, host=host)
    if kind == "info":
        # 只读取身份（SN / UID），供部署脚本自动补全配置。不写设备。
        cfg = load_config()
        host = (cfg.get("device") or {}).get("host")
        if not host:
            die("配置里没有 device.host")
        mod = load_module("minas_root_info", find_root_tool())
        try:
            nas = mod.Nas(host, str(cert_paths(cfg)))
        except Exception as exc:
            die("证书会话失败：%s" % exc)
        print("SN=%s" % (nas.sn or ""))
        print("UID=%s" % (nas.uid or ""))
        return 0
    if kind == "deploy":
        cfg = load_config()
        # 通道按现场情况选：刚 root 完 2323 还活着 → socat（对 dropbear 的
        # exec bug 免疫）；设备已部署过、2323 已关闭 → 走已固化的 SSH。
        device = cfg.get("device") or {}
        host = device.get("host") or ""
        shell_port = int(device.get("shell_port") or 2323)
        channel = "socat" if tcp_open(host, shell_port, timeout=3) else "ssh"
        print("部署通道：%s（%s）" % (channel, "2323 root shell 在线" if channel == "socat" else "2323 不在线，走 SSH"))
        write_deploy_config(cfg, channel)
        mod = load_module("minas_deploy", find_deploy_tool())
        patch_deploy(mod)
        if "--channel" not in rest:
            rest = rest + ["--channel", channel]
        return mod.main(argv=rest)
    if kind == "root":
        cfg = load_config()
        host = (cfg.get("device") or {}).get("host")
        if not host:
            die("配置里没有 device.host")
        if not rest:
            rest = [host]
        path = find_root_tool()
        mod = load_module("minas_root", path)
        patch_root(mod)
        sys.argv = [str(path)] + rest
        mod.main()
        return 0
    if kind == "exec":
        path = None
        env = {}
        timeout = 180
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--file" and i + 1 < len(rest):
                path = Path(rest[i + 1]); i += 2
            elif token == "--env" and i + 1 < len(rest):
                key, _, value = rest[i + 1].partition("=")
                env[key] = value; i += 2
            elif token == "--timeout" and i + 1 < len(rest):
                timeout = int(rest[i + 1]); i += 2
            else:
                i += 1
        if path is None:
            die("exec 需要 --file <脚本路径>")
        if not path.is_file():
            die("脚本不存在：%s" % path)
        output = run_script(path.read_text(encoding="utf-8"), env=env, timeout=timeout)
        print(output)
        return 0
    print("未知模式：%s（可用 deploy / root / probe / exec / selftest）" % kind, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
