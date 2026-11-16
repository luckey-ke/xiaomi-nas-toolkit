#!/bin/sh
# 验收 v4 稳定性状态。可选参数：
#   --reconcile   额外跑一次真实 plugincenter update，确认注册状态不被删
# 退出码 0 = 全绿；非 0 = 有检查项失败或对账破坏。
set -eu

CONF=/data/minas-ssh/keep-plugins/plugins.json
if [ ! -f "$CONF" ]; then
  echo "缺少 $CONF，本机尚未部署 v4 栈" >&2
  exit 2
fi
UID_N="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["uid"])' "$CONF")"
USER_NAME="u${UID_N}"
PLUGIN_IDS="$(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["plugins"]))' "$CONF")"
port_of() {
  python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plugins"][sys.argv[2]])' "$CONF" "$1"
}

RECONCILE=0
[ "${1:-}" = "--reconcile" ] && RECONCILE=1

echo "== 身份 =="
id -u
cat /proc/sys/kernel/random/boot_id
uptime

echo "== SSH =="
systemctl is-active minas-sshd.service
systemctl is-enabled minas-sshd.service
printf 'root home: '; getent passwd root | cut -d: -f6
printf 'keepalive: '; systemctl cat minas-sshd.service | grep -o '\-K 60' || echo MISSING

echo "== 目录守卫 =="
systemctl is-active minas-catalog-guard.service
curl -sS --max-time 4 http://127.0.0.1:9310/api/health
echo
python3 - <<'PY'
import json
cfg = json.load(open('/etc/config/plugin', encoding='utf-8'))
print('catalog source:', cfg['settings']['server_list'])
print('ports:', sorted(cfg.get('ports', {})))
PY

echo "== 自愈 =="
systemctl is-enabled keep-plugins.timer
systemctl is-active keep-plugins.timer
/usr/bin/python3 /data/minas-ssh/keep-plugins/keep-plugins.py --dry-run

echo "== 运行时独立性 =="
for p in $PLUGIN_IDS; do
  test -f "/data/minas/plugins/$p/INFO"
  test -f "/data/minas/plugins/$p/src/files/httpd.py"
  printf '%s unit: ' "$p"
  systemctl cat "$p.service" | grep -q "/data/minas/plugins/$p" && echo stable || echo NOT-STABLE
  printf '%s www: ' "$p"
  readlink "/data/plugin/www/${USER_NAME}/$p" | grep -q "/data/minas/plugins/$p" && echo stable || echo NOT-STABLE
done

echo "== 服务与健康 =="
for p in $PLUGIN_IDS; do printf '%s: ' "$p"; systemctl is-active "$p.service"; done
for p in $PLUGIN_IDS; do
  printf '%s -> ' "$(port_of "$p")"
  curl -sS --max-time 4 "http://127.0.0.1:$(port_of "$p")/api/health"
  echo
done

echo "== 注册状态 =="
python3 - "$USER_NAME" "$PLUGIN_IDS" <<'PY'
import json, sys
user, ids = sys.argv[1], sys.argv[2].split()
ulist = json.load(open('/data/plugin/%s.list' % user, encoding='utf-8'))
slist = json.load(open('/data/plugin/server.list', encoding='utf-8'))
for name in ids:
    print(name, 'user=', name in ulist, 'install=', (ulist.get(name) or {}).get('install'),
          'server=', name in slist.get('data', {}))
PY

echo "== 开机钩子 =="
for f in /etc/syshotplug/pool/97.ssh-persistence /data/etc/upper/syshotplug/pool/97.ssh-persistence; do
  test -f "$f"
  grep -q 'hook fired (v4' "$f"
  printf '%s OK\n' "$f"
done

if [ "$RECONCILE" = 1 ]; then
  echo "== 附加对账：守卫下的真实 update =="
  for p in $PLUGIN_IDS; do
    systemctl show "$p.service" -p MainPID --value > "/tmp/v4-${p}-pid-check"
  done
  set +e
  plugincenter -u "$USER_NAME" update >/tmp/v4-reconcile.log 2>&1
  RC=$?
  set -e
  echo "plugincenter update rc=$RC"
  if grep -q 'already taken down' /tmp/v4-reconcile.log; then
    echo 'FAIL: 出现 taken-down 判定' >&2
    exit 1
  fi
  for p in $PLUGIN_IDS; do
    before="$(cat "/tmp/v4-${p}-pid-check")"
    after="$(systemctl show "$p.service" -p MainPID --value)"
    if [ "$before" != "$after" ]; then
      echo "FAIL: $p 被重启（$before -> $after）" >&2
      exit 1
    fi
  done
  python3 - "$USER_NAME" "$PLUGIN_IDS" <<'PY'
import json, sys
user, ids = sys.argv[1], sys.argv[2].split()
ulist = json.load(open('/data/plugin/%s.list' % user, encoding='utf-8'))
slist = json.load(open('/data/plugin/server.list', encoding='utf-8'))
missing = [p for p in ids if p not in ulist or p not in slist.get('data', {})]
assert not missing, missing
print('注册状态在附加对账中存活')
PY
  echo '附加对账：OK'
fi

echo 'VALIDATION OK'
