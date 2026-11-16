#!/bin/sh
# 在 NAS 上部署 v4 稳定性栈。以 root 运行，参数依次为：
#   $1 设备 SN（仅用于校验目录守卫的合并结果，可传 unknown）
#   $2 暂存目录（可选，默认 /data/minas/plugins/catalog/stage）
#
# 前置：本脚本同目录下已有 deploy.json 与全部组件（由 windows/deploy.ps1 上传）。
# deploy.json 是本栈唯一的配置来源，脚本内不硬编码任何 UID / 端口 / IP。
set -eu

SN="${1:-unknown}"
STAGE="${2:-/data/minas/plugins/catalog/stage}"
CFG="$STAGE/deploy.json"
[ -f "$CFG" ] || { echo "缺少配置：$CFG" >&2; exit 2; }

KEEP_DIR=/data/minas-ssh/keep-plugins
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/data/minas-ssh/backup-v4-${STAMP}"
mkdir -p "$BACKUP" /data/minas/plugins/catalog "$KEEP_DIR"

echo "== [0/9] 解析配置 =="
python3 - "$CFG" "$KEEP_DIR" <<'PY'
import json, os, pathlib, sys
cfg_path, keep_dir = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_path, encoding="utf-8"))
dev = cfg.get("device", {})
plugins = (cfg.get("plugins") or {}).get("enabled") or []
ports = (cfg.get("plugins") or {}).get("ports") or {}
if not dev.get("uid"):
    sys.exit("deploy.json 缺少 device.uid")
if not plugins:
    sys.exit("deploy.json 缺少 plugins.enabled")
mapping = {}
for pid in plugins:
    if pid not in ports:
        sys.exit("插件 %s 缺少端口配置" % pid)
    mapping[pid] = int(ports[pid])
conf = {
    "uid": str(dev["uid"]),
    "plugins": mapping,
    "allowed_ips": (cfg.get("access") or {}).get("allowed_ips") or [],
}
pathlib.Path(keep_dir).mkdir(parents=True, exist_ok=True)
out = pathlib.Path(keep_dir, "plugins.json")
out.write_text(json.dumps(conf, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("    uid=%s plugins=%s" % (conf["uid"], ",".join("%s:%d" % kv for kv in mapping.items())))
PY

UID_N="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["uid"])' "$KEEP_DIR/plugins.json")"
USER_NAME="u${UID_N}"
PLUGIN_IDS="$(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["plugins"]))' "$KEEP_DIR/plugins.json")"
GUARD_PORT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("guard_port",9310))' "$CFG" 2>/dev/null || echo 9310)"
GUARD_URL="http://127.0.0.1:${GUARD_PORT}/ota-server/version/plugin/get"

echo "== [1/9] 备份当前状态到 $BACKUP =="
BACKUP_FILES="/etc/config/plugin /data/plugin/${USER_NAME}.list /data/plugin/server.list \
/etc/syshotplug/pool/97.ssh-persistence /etc/systemd/system/minas-sshd.service \
/etc/systemd/system/keep-plugins.service /etc/systemd/system/keep-plugins.timer \
/etc/systemd/system/minas-catalog-guard.service /etc/nginx/conf.d/luci/plugincenter.conf"
for p in $PLUGIN_IDS; do
  BACKUP_FILES="$BACKUP_FILES /etc/systemd/system/$p.service"
done
for f in $BACKUP_FILES; do
  [ -f "$f" ] || continue
  mkdir -p "$BACKUP$(dirname "$f")"
  cp -a "$f" "$BACKUP$f"
done
echo "    备份完成"

echo "== [2/9] 安装稳定文件（不打断运行中的服务）=="
install -m 755 "$STAGE/catalog_guard.py" /data/minas/plugins/catalog/catalog_guard.py
install -m 755 "$STAGE/keep_plugins_v4.py" "$KEEP_DIR/keep-plugins.py"
install -m 755 "$STAGE/keepctl.sh" "$KEEP_DIR/keepctl.sh"
if [ -f "$STAGE/使用说明.md" ]; then
  install -m 644 "$STAGE/使用说明.md" "$KEEP_DIR/使用说明.md"
fi
install -m 755 "$STAGE/hook_v4.sh" /etc/syshotplug/pool/97.ssh-persistence
install -m 644 "$STAGE/units/minas-catalog-guard.service" /etc/systemd/system/minas-catalog-guard.service
install -m 644 "$STAGE/units/keep-plugins.service" /etc/systemd/system/keep-plugins.service
install -m 644 "$STAGE/units/keep-plugins.timer" /etc/systemd/system/keep-plugins.timer
mkdir -p /data/etc/upper/syshotplug/pool
install -m 755 "$STAGE/hook_v4.sh" /data/etc/upper/syshotplug/pool/97.ssh-persistence
# keep.conf 由配置生成，避免部署完还要手工改调度
python3 - "$CFG" "$KEEP_DIR/keep.conf" <<'PY'
import json, pathlib, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
sh = cfg.get("selfheal") or {}
mode = sh.get("mode") or "calendar"
if mode not in ("calendar", "interval"):
    mode = "calendar"
lines = [
    "# MINAS 自愈定时配置（由部署脚本生成）",
    "# MODE     : interval | calendar",
    "# INTERVAL : 间隔模式周期（30s / 2min / 10min / 1h）",
    "# CALENDAR : 日历模式（08,12,15,18:00 / 03:00 / \"Mon..Fri 09:00\"）",
    "# ENABLED  : yes = 定时器启用并开机自启",
    "# 改完后执行：keepctl apply    （或用 keepctl set / keepctl at）",
    "MODE=%s" % mode,
    "INTERVAL=%s" % (sh.get("interval") or "2min"),
    "CALENDAR=%s" % (sh.get("calendar") or "08,12,15,18:00"),
    "ENABLED=yes",
]
pathlib.Path(sys.argv[2]).write_text("\n".join(lines) + "\n", encoding="utf-8")
print("    keep.conf: mode=%s" % mode)
PY
systemctl daemon-reload

echo "== [3/9] 建立稳定运行时与目录条目 =="
/usr/bin/python3 "$KEEP_DIR/keep-plugins.py"
test -f /data/minas/plugins/catalog/entries.json

echo "== [4/9] 启动目录守卫并校验合并结果 =="
systemctl enable minas-catalog-guard.service >/dev/null 2>&1
systemctl restart minas-catalog-guard.service
sleep 2
systemctl is-active --quiet minas-catalog-guard.service
BODY="{\"companyCode\":\"XiaomiNAS\",\"model\":\"xiaomi.station.rp05\",\"countryCode\":\"CN\",\"sn\":\"$SN\",\"versionCode\":\"1.0.9.464\"}"
HTTP_CODE="$(curl -sS -o /tmp/v4-guard-response.json -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -d "$BODY" \
  "http://127.0.0.1:${GUARD_PORT}/ota-server/version/plugin/get")"
echo "    守卫 HTTP $HTTP_CODE"
python3 - "$KEEP_DIR/plugins.json" <<'PY'
import json, sys
reply = json.load(open('/tmp/v4-guard-response.json', encoding='utf-8'))
data = reply.get('data')
if reply.get('code') != 0 or not isinstance(data, dict):
    sys.exit('守卫响应异常：%s' % str(reply)[:300])
want = list(json.load(open(sys.argv[1], encoding='utf-8'))['plugins'])
missing = [p for p in want if p not in data]
if missing:
    sys.exit('守卫未合并：%s' % missing)
print('    已合并条目：%s' % [p for p in want if p in data])
PY

echo "== [5/9] 把 plugincenter 的目录源指向本地守卫 =="
python3 - "$BACKUP" "$GUARD_URL" <<'PY'
import json, os, pathlib, sys
backup, url = sys.argv[1], sys.argv[2]
path = pathlib.Path('/etc/config/plugin')
data = json.loads(path.read_text(encoding='utf-8'))
settings = data.setdefault('settings', {})
old = settings.get('server_list')
if old and old != url:
    pathlib.Path(backup, 'server_list.previous').write_text(old, encoding='utf-8')
settings['server_list'] = url
temp = path.with_name(path.name + '.tmp-v4')
temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
os.replace(temp, path)
print('    server_list: %s -> %s' % (old, url))
PY
systemctl is-active --quiet minas-catalog-guard.service

echo "== [6/9] 对账测试：在守卫下跑一次真实 plugincenter update =="
for p in $PLUGIN_IDS; do
  systemctl show "$p.service" -p MainPID --value > "/tmp/v4-${p}-pid-before"
done
BEFORE_DIRS="$(for p in $PLUGIN_IDS; do test -f "/home/${USER_NAME}/plugin/$p/INFO" && printf '%s=present ' "$p" || printf '%s=missing ' "$p"; done)"
set +e
plugincenter -u "$USER_NAME" update >/tmp/v4-plugincenter-update.log 2>&1
UPDATE_RC=$?
set -e
echo "    plugincenter update rc=$UPDATE_RC"
echo "    目录前：$BEFORE_DIRS"
AFTER_DIRS="$(for p in $PLUGIN_IDS; do test -f "/home/${USER_NAME}/plugin/$p/INFO" && printf '%s=present ' "$p" || printf '%s=missing ' "$p"; done)"
echo "    目录后：$AFTER_DIRS"
python3 - "$KEEP_DIR/plugins.json" "$USER_NAME" <<'PY'
import json, sys
want = list(json.load(open(sys.argv[1], encoding='utf-8'))['plugins'])
user = sys.argv[2]
ulist = json.load(open('/data/plugin/%s.list' % user, encoding='utf-8'))
slist = json.load(open('/data/plugin/server.list', encoding='utf-8'))
missing_user = [p for p in want if p not in ulist]
missing_server = [p for p in want if p not in slist.get('data', {})]
if missing_user or missing_server:
    sys.exit('对账删除了条目：user=%s server=%s' % (missing_user, missing_server))
print('    注册状态在对账中存活')
PY
for p in $PLUGIN_IDS; do
  before="$(cat "/tmp/v4-${p}-pid-before")"
  after="$(systemctl show "$p.service" -p MainPID --value)"
  if [ "$before" != "$after" ]; then
    echo "服务 $p 在对账期间被重启（$before -> $after），中止迁移" >&2
    exit 1
  fi
done
if grep -q 'already taken down' /tmp/v4-plugincenter-update.log; then
  echo 'plugincenter 仍判定插件被下架，中止' >&2
  exit 1
fi

echo "== [7/9] 把服务迁移到稳定运行时 =="
for p in $PLUGIN_IDS; do
  systemctl restart "$p.service"
done
sleep 6
for p in $PLUGIN_IDS; do
  port="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plugins"][sys.argv[2]])' "$KEEP_DIR/plugins.json" "$p")"
  curl -sS --max-time 4 "http://127.0.0.1:${port}/api/health" >/dev/null || {
    echo "插件 $p 健康检查失败（端口 $port）" >&2; exit 1; }
done
echo "    稳定运行时健康检查通过"

echo "== [8/9] 清理旧 restore 单元并应用定时策略 =="
# minas-sshd.service 由 ssh_persist.sh 写（带 -s/密码/keepalive 等运行期决定），
# 这里刻意不覆盖，否则会丢掉认证配置。
for p in $PLUGIN_IDS; do
  systemctl disable "$p-restore.service" >/dev/null 2>&1 || true
  systemctl stop "$p-restore.service" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$p-restore.service"
done
systemctl daemon-reload
chmod 755 "$KEEP_DIR/keepctl.sh"
"$KEEP_DIR/keepctl.sh" apply

echo "== [9/9] 最终状态 =="
systemctl is-active minas-sshd.service
systemctl is-active minas-catalog-guard.service
systemctl is-enabled keep-plugins.timer
for p in $PLUGIN_IDS; do
  printf '%s: ' "$p"; systemctl is-active "$p.service"
done
python3 - "$USER_NAME" <<'PY'
import json, sys
user = sys.argv[1]
cfg = json.load(open('/etc/config/plugin', encoding='utf-8'))
print('    目录源：%s' % cfg['settings']['server_list'])
print('    ports：%s' % sorted(cfg.get('ports', {})))
ulist = json.load(open('/data/plugin/%s.list' % user, encoding='utf-8'))
print('    已安装：%s' % {k: v.get('install') for k, v in ulist.items() if v.get('install')})
PY
echo "v4 部署完成；备份位于 $BACKUP"
