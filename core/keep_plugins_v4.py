#!/usr/bin/env python3
"""MINAS 插件运行时自愈（v4）。

设计要点（对应 2026-11-15 对抗性复查的结论）：

1. 云端 server.list 是插件存活的唯一真源。本地目录守卫（catalog_guard）
   会把自研插件合并进每次目录响应；本脚本再对磁盘上的 server.list 做
   一次兜底修复。
2. 服务代码与网页不再从 /home/<uid>/plugin 运行——该目录厂商有权重建或
   删除。运行时统一放在 /data/minas/plugins 下，与厂商对账解耦。
3. 不调用 restore.sh，因此共享 flock 不可能自死锁。
4. 单元文件内容不变则不重写；常规自愈期间绝不重启正在运行的服务。

配置来源：/data/minas-ssh/keep-plugins/plugins.json（由部署脚本生成）
  {
    "uid": "<你的米家数字 UID>",
    "plugins": {"dockerctl": 9302, "sysmon": 9301, "pcbackup": 9303},
    "allowed_ips": ["<你的网段>/24"]
  }

用法：
  python3 keep_plugins_v4.py              正常自愈
  python3 keep_plugins_v4.py --dry-run    只报告将要做的改动，不写盘
  python3 keep_plugins_v4.py --apply-restarts
                                          单元迁移后重启受影响服务（仅一次性迁移用）
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

CONF_PATH = pathlib.Path(
    os.environ.get("MINAS_PLUGIN_CONF", "/data/minas-ssh/keep-plugins/plugins.json")
)
RUNTIME_ROOT = pathlib.Path("/data/minas/plugins")
BACKUP_ROOT = pathlib.Path("/data/backup/plugins")
WWW_ROOT = pathlib.Path("/data/plugin/www")
ICON_DIR = pathlib.Path("/data/plugin/www/icon")
SERVER_LIST = pathlib.Path("/data/plugin/server.list")
PORTS_FILE = pathlib.Path("/etc/config/plugin")
ENTRIES_FILE = pathlib.Path("/data/minas-ssh/keep-plugins/entries.json")
CATALOG_ENTRIES = pathlib.Path("/data/minas/plugins/catalog/entries.json")
NGINX_CONF = pathlib.Path("/etc/nginx/conf.d/luci/plugincenter.conf")
LOCK = "/run/lock/minas-plugin-restore.lock"
UNIT_DIR = pathlib.Path("/etc/systemd/system")
IPT = "export PATH=/usr/sbin:/sbin:$PATH; iptables "

DRY_RUN = False


class Config:
    """运行期参数：全部来自 plugins.json，不在代码里硬编码任何设备信息。"""

    def __init__(self, uid, plugins, allowed_ips):
        self.uid = str(uid)
        self.user = "u%s" % self.uid
        self.plugins = dict(plugins)
        self.allowed_ips = list(allowed_ips)
        self.home_root = pathlib.Path("/home/%s/plugin" % self.user)
        self.www_dir = WWW_ROOT / self.user
        self.list_file = pathlib.Path("/data/plugin/%s.list" % self.user)


def load_config():
    try:
        with open(CONF_PATH, encoding="utf-8") as stream:
            raw = json.load(stream)
    except OSError:
        print("[keep] 缺少配置 %s，请先运行部署脚本" % CONF_PATH)
        return None
    except ValueError as exc:
        print("[keep] 配置不是合法 JSON：%s" % exc)
        return None
    uid = raw.get("uid")
    plugins = raw.get("plugins")
    if not uid or not isinstance(plugins, dict) or not plugins:
        print("[keep] 配置缺少 uid 或 plugins")
        return None
    parsed = {}
    for pid, port in plugins.items():
        try:
            parsed[str(pid)] = int(port)
        except (TypeError, ValueError):
            print("[keep] 插件 %s 的端口非法：%r" % (pid, port))
            return None
    return Config(uid, parsed, raw.get("allowed_ips") or [])


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def atomic_write(path, text):
    path = pathlib.Path(path)
    if path.parent != pathlib.Path("/"):
        path.parent.mkdir(parents=True, exist_ok=True)
    if DRY_RUN:
        print("[keep] (dry-run) would write %s (%d bytes)" % (path, len(text)))
        return
    temp = path.with_name("%s.tmp-keep-%d" % (path.name, os.getpid()))
    mode = 0o644
    uid = gid = None
    try:
        stat = path.stat()
        mode = stat.st_mode & 0o777
        uid, gid = stat.st_uid, stat.st_gid
    except OSError:
        pass
    with open(temp, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    if uid is not None:
        try:
            os.chown(temp, uid, gid)
        except OSError:
            pass
    os.replace(temp, path)


def write_json_if_changed(path, data):
    old = load_json(path, None)
    if old == data:
        return False
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return True


def sh(cmd, timeout=30):
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)


def unit_text(cfg, pid, port):
    runtime = RUNTIME_ROOT / pid
    httpd = runtime / "src" / "files" / "httpd.py"
    logdir = "/tmp/%s" % pid
    if pid == "dockerctl":
        start = "/bin/sh -c 'exec /usr/bin/python3 %s >>%s/server.log 2>&1'" % (httpd, logdir)
        environment = (
            "Environment=PLUG_PORT=%d\n"
            "Environment=DOCKER_BIN=/data/docker/docker\n"
            "Environment=SKOPEO_BIN=%s\n" % (port, runtime / "scripts" / "skopeo-container")
        )
    elif pid == "pcbackup":
        start = "/usr/bin/python3 %s" % httpd
        environment = (
            "Environment=PLUG_PORT=%d\n"
            "Environment=PCBACKUP_DATA=/data/backup/plugins/pcbackup/state\n"
            "Environment=PLUG_USER=%s\n" % (port, cfg.user)
        )
    else:
        start = "/usr/bin/python3 %s" % httpd
        environment = "Environment=PLUG_PORT=%d\n" % port
    return (
        "[Unit]\n"
        "Description=%s service (minas runtime v4)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "%s"
        "StandardOutput=append:%s/server.log\n"
        "StandardError=append:%s/server.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n" % (pid, start, environment, logdir, logdir)
    )


def nginx_block(cfg, pid, port):
    if pid == "pcbackup":
        limits = (
            "  client_max_body_size 256m;\n"
            "  proxy_request_buffering off;\n"
            "  proxy_read_timeout 3600s;\n"
            "  proxy_send_timeout 3600s;\n"
        )
    elif pid == "dockerctl":
        limits = (
            "  client_max_body_size 16m;\n"
            "  proxy_read_timeout 330s;\n"
            "  proxy_send_timeout 330s;\n"
        )
    else:
        limits = "  proxy_read_timeout 330s;\n  proxy_send_timeout 330s;\n"
    return (
        "# === %s (auto-added) ===\n"
        "location ~ ^/plugin/u?%s/%s/api/ {\n"
        "  rewrite ^/plugin/u?%s/%s/(api/.*)$ /$1 break;\n"
        "  proxy_pass http://127.0.0.1:%d;\n"
        "  proxy_set_header Host $host;\n"
        "  proxy_set_header X-Real-IP $remote_addr;\n"
        "  proxy_set_header X-Forwarded-Proto https;\n"
        "  proxy_http_version 1.1;\n"
        "%s"
        "  add_header Cache-Control \"no-store\";\n"
        "}\n"
        "# === /%s ===\n" % (pid, cfg.uid, pid, cfg.uid, pid, port, limits, pid)
    )


def ensure_runtime(cfg, pid):
    source = BACKUP_ROOT / pid / pid
    target = RUNTIME_ROOT / pid
    if not (source / "INFO").is_file():
        print("[keep] %s 的运行时源缺失（%s）" % (pid, source))
        return False
    if not (target / "INFO").is_file():
        if DRY_RUN:
            print("[keep] (dry-run) would install runtime for %s" % pid)
            return True
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copytree(source, target, symlinks=True)
        print("[keep] 已安装稳定运行时：%s" % pid)
    for name in ("scripts/control", "scripts/skopeo-container",
                 "src/files/server.sh", "src/ui/%s.cgi" % pid):
        candidate = target / name
        if candidate.is_file():
            os.chmod(candidate, 0o755)
    return True


def ensure_home(cfg, pid):
    """维护兼容目录与网页软链。

    单文件部署（deploy-plugins.py）仍要求 /home/<user>/plugin/<pid> 存在，
    因此这里补一份副本；但服务本身跑在 RUNTIME_ROOT 上，厂商清空该目录
    也不影响运行。
    """
    runtime = RUNTIME_ROOT / pid
    home = cfg.home_root / pid
    if not (runtime / "INFO").is_file():
        return False
    if not (home / "INFO").is_file():
        if DRY_RUN:
            print("[keep] (dry-run) would restore compat home for %s" % pid)
        else:
            cfg.home_root.mkdir(parents=True, exist_ok=True)
            if home.exists() or home.is_symlink():
                if home.is_dir() and not home.is_symlink():
                    shutil.rmtree(home)
                else:
                    home.unlink()
            shutil.copytree(runtime, home, symlinks=True)
            try:
                shutil.chown(home, user=cfg.user, group=cfg.user)
                for root, dirs, files in os.walk(home):
                    for name in dirs + files:
                        try:
                            shutil.chown(os.path.join(root, name), user=cfg.user, group=cfg.user)
                        except OSError:
                            pass
            except OSError:
                pass
            print("[keep] 已恢复兼容目录：%s" % pid)
    for name in ("scripts/control", "scripts/skopeo-container",
                 "src/files/server.sh", "src/ui/%s.cgi" % pid):
        candidate = home / name
        if candidate.is_file():
            os.chmod(candidate, 0o755)

    www = cfg.www_dir / pid
    wanted = str(runtime / "src" / "ui")
    try:
        if www.is_symlink() and os.readlink(www) == wanted:
            return True
    except OSError:
        pass
    if DRY_RUN:
        print("[keep] (dry-run) would link UI for %s" % pid)
        return True
    cfg.www_dir.mkdir(parents=True, exist_ok=True)
    temp = cfg.www_dir / (".%s.tmp-keep" % pid)
    if temp.exists() or temp.is_symlink():
        if temp.is_dir() and not temp.is_symlink():
            shutil.rmtree(temp)
        else:
            temp.unlink()
    os.symlink(wanted, temp)
    os.replace(temp, www)
    print("[keep] 已链接稳定 UI：%s" % pid)
    icon_source = runtime / "src" / "ui" / "icon.png"
    if icon_source.is_file():
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon_source, ICON_DIR / ("%s.icon" % pid))
    return True


def ensure_registrations(cfg, entries):
    changed = []
    data = load_json(cfg.list_file, None)
    if not isinstance(data, dict):
        data = {}
    list_changed = False
    state_fields = ("status", "install", "upgrade", "enable", "online",
                    "autograde", "icon", "progress", "frontend", "changetime")
    for pid in cfg.plugins:
        entry = entries.get(pid)
        if not isinstance(entry, dict):
            continue
        current = data.get(pid)
        if current is None:
            data[pid] = entry
            list_changed = True
            print("[keep] 已恢复用户清单条目：%s" % pid)
            continue
        # plugincenter 会把目录里的插件按全新条目重新初始化
        # （install=false / status=free）；这里修回状态位，否则 App 会
        # 把它显示成"需要下载"，点下载又必然失败。
        repaired = False
        for field in state_fields:
            if field in entry and current.get(field) != entry.get(field):
                current[field] = entry[field]
                repaired = True
        if repaired:
            list_changed = True
            print("[keep] 已修复用户清单状态：%s" % pid)
    if list_changed:
        atomic_write(cfg.list_file, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        changed.append("ulist")

    catalog = load_json(SERVER_LIST, None)
    if isinstance(catalog, dict) and isinstance(catalog.get("data"), dict):
        server_changed = False
        for pid in cfg.plugins:
            if pid not in catalog["data"] and pid in entries:
                entry = entries[pid]
                catalog["data"][pid] = {
                    "info": entry.get("info"),
                    "resource": entry.get("resource"),
                }
                server_changed = True
                print("[keep] 已恢复 server.list 条目：%s" % pid)
        if server_changed:
            atomic_write(SERVER_LIST, json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))
            changed.append("serverlist")
    else:
        # server.list 缺失或不是合法 JSON：plugincenter 是「先截断、再下载写入」，
        # 云端一旦抖动就会留下 0 字节空文件（2026-11-16 实测发生）。
        # 目录内容有守卫兜底，直接让 plugincenter 重新拉取重建即可。
        if DRY_RUN:
            print("[keep] (dry-run) would rebuild server.list via plugincenter update")
        else:
            print("[keep] server.list 缺失/损坏，触发 plugincenter 重建")
            sh("plugincenter -u %s -p all update >/dev/null 2>&1" % cfg.user, timeout=180)
            changed.append("serverlist-rebuild")

    ports = load_json(PORTS_FILE, None)
    if isinstance(ports, dict):
        table = ports.setdefault("ports", {})
        ports_changed = False
        for pid, port in cfg.plugins.items():
            wanted = {cfg.user: port}
            if table.get(pid) != wanted:
                table[pid] = wanted
                ports_changed = True
        if ports_changed:
            atomic_write(PORTS_FILE, json.dumps(ports, ensure_ascii=False, indent=1) + "\n")
            print("[keep] 已更新 ports 注册表")
            changed.append("ports")
    return changed


def ensure_catalog_entries(cfg, entries):
    catalog = {}
    for pid, entry in entries.items():
        if pid in cfg.plugins:
            catalog[pid] = {"info": entry.get("info"), "resource": entry.get("resource")}
    if write_json_if_changed(CATALOG_ENTRIES, catalog):
        print("[keep] 已更新目录守卫条目")


def ensure_units(cfg, apply_restarts=False):
    if DRY_RUN:
        for pid in cfg.plugins:
            print("[keep] (dry-run) would verify unit %s.service" % pid)
        return []
    changed = []
    for pid, port in cfg.plugins.items():
        unit = UNIT_DIR / ("%s.service" % pid)
        wanted = unit_text(cfg, pid, port)
        current = None
        try:
            current = unit.read_text(encoding="utf-8")
        except OSError:
            pass
        if current != wanted:
            atomic_write(unit, wanted)
            changed.append(pid)
            print("[keep] 已更新单元：%s" % pid)
    if changed:
        sh("systemctl daemon-reload", timeout=60)
    for pid in cfg.plugins:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "%s.service" % pid]
        ).returncode == 0
        if not active:
            subprocess.run(
                ["systemctl", "--no-block", "start", "%s.service" % pid],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print("[keep] 已启动服务：%s" % pid)
        elif apply_restarts and pid in changed:
            subprocess.run(
                ["systemctl", "--no-block", "restart", "%s.service" % pid],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print("[keep] 单元迁移后重启服务：%s" % pid)
    return changed


def ensure_nginx(cfg):
    if DRY_RUN:
        print("[keep] (dry-run) would verify nginx locations")
        return []
    changed = []
    try:
        text = NGINX_CONF.read_text(encoding="utf-8")
    except OSError:
        return changed
    for pid, port in cfg.plugins.items():
        marker = "# === %s (auto-added) ===" % pid
        if marker in text:
            continue
        block = nginx_block(cfg, pid, port)
        output = []
        inserted = False
        for line in text.splitlines(keepends=True):
            if not inserted and line.lstrip().startswith("location /plugin"):
                output.append(block)
                inserted = True
            output.append(line)
        if not inserted:
            output.append(block)
        text = "".join(output)
        changed.append(pid)
    if changed:
        atomic_write(NGINX_CONF, text)
        result = sh("nginx -t && nginx -s reload", timeout=60)
        if result.returncode != 0:
            print("[keep] nginx 重载失败：%s" % result.stderr.strip()[:120])
        else:
            print("[keep] 已恢复 nginx 路由：%s" % ",".join(changed))
    return changed


def ensure_2323_guard(cfg):
    """2323 root shell 只允许白名单来源；规则会被厂商服务操作清空，需每轮复查。"""
    if not cfg.allowed_ips:
        return
    if DRY_RUN:
        print("[keep] (dry-run) would verify 2323 source restriction")
        return
    result = sh(IPT + "-S INPUT 2>/dev/null", timeout=15)
    if "dport 2323" in result.stdout:
        return
    idx = 1
    for ip in cfg.allowed_ips:
        sh(IPT + "-I INPUT %d -p tcp --dport 2323 -s %s -j ACCEPT 2>/dev/null" % (idx, ip), timeout=15)
        idx += 1
    sh(IPT + "-I INPUT %d -p tcp --dport 2323 -j DROP 2>/dev/null" % idx, timeout=15)
    print("[keep] 已重新应用 2323 来源限制")


def main():
    global DRY_RUN
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只报告改动，不写盘")
    parser.add_argument("--apply-restarts", action="store_true",
                        help="重启单元内容变化的服务（仅一次性迁移用）")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    cfg = load_config()
    if cfg is None:
        return 2

    os.makedirs("/run/lock", exist_ok=True)
    # fcntl 仅 Unix 有；放到这里导入，便于在 Windows 上做离线单测
    import fcntl
    with open(LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[keep] 另一个自愈进程正在运行，跳过")
            return 0

        entries = load_json(ENTRIES_FILE, {})
        if not entries:
            for pid in cfg.plugins:
                data = load_json(BACKUP_ROOT / pid / "list-entry.json", {})
                if pid in data:
                    entries[pid] = data[pid]
        if not entries:
            print("[keep] 没有缓存的插件条目，中止")
            return 1

        for pid in cfg.plugins:
            pathlib.Path("/tmp/%s" % pid).mkdir(parents=True, exist_ok=True)
            if not ensure_runtime(cfg, pid):
                return 1
            ensure_home(cfg, pid)
        ensure_catalog_entries(cfg, entries)
        ensure_registrations(cfg, entries)
        ensure_units(cfg, args.apply_restarts)
        ensure_nginx(cfg)
        ensure_2323_guard(cfg)
        if DRY_RUN:
            print("[keep] dry-run 结束，未改动任何文件")
        return 0


if __name__ == "__main__":
    sys.exit(main())
