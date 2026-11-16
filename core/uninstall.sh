#!/bin/sh
# 卸载本工具加装的东西，尽量把设备退回接近原厂的状态。
# 在 NAS 上以 root 运行：sh /data/minas/plugins/catalog/stage/uninstall.sh [--purge]
#
# 默认保留：/data 下的备份目录、插件运行时快照（便于回滚）
# --purge  一并删除 /data/minas 与 /data/plugin/www 下的软链
set -eu

KEEP_DIR=/data/minas-ssh/keep-plugins
CONF="$KEEP_DIR/plugins.json"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ -f "$CONF" ]; then
  PLUGIN_IDS="$(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["plugins"]))' "$CONF")"
  USER_NAME="$(python3 -c 'import json,sys;print("u"+json.load(open(sys.argv[1]))["uid"])' "$CONF")"
else
  PLUGIN_IDS=""
  USER_NAME=""
  echo "警告：读不到 $CONF，将只清理 SSH 与守卫组件"
fi

echo "== 1. 停用并移除自愈定时器 =="
systemctl disable --now keep-plugins.timer 2>/dev/null || true
systemctl stop keep-plugins.service 2>/dev/null || true
rm -f /etc/systemd/system/keep-plugins.timer /etc/systemd/system/keep-plugins.service

echo "== 2. 停用并移除目录守卫 =="
systemctl disable --now minas-catalog-guard.service 2>/dev/null || true
rm -f /etc/systemd/system/minas-catalog-guard.service

echo "== 3. 还原 plugincenter 的目录源 =="
python3 - <<'PY'
import json, pathlib
path = pathlib.Path('/etc/config/plugin')
data = json.loads(path.read_text(encoding='utf-8'))
settings = data.get('settings') or {}
current = settings.get('server_list') or ''
backups = sorted(pathlib.Path('/data/minas-ssh').glob('backup-v4-*/server_list.previous'))
restored = None
if backups:
    restored = backups[-1].read_text(encoding='utf-8').strip()
if '127.0.0.1' in current or current.startswith('http://127.'):
    if restored:
        settings['server_list'] = restored
        print('已还原目录源为：%s' % restored)
    else:
        settings.pop('server_list', None)
        print('未找到历史值，已移除 server_list 覆盖项')
    temp = path.with_name(path.name + '.tmp-uninstall')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    temp.replace(path)
else:
    print('目录源未被本工具改写（%s），跳过' % (current or '未设置'))
PY

echo "== 4. 移除插件单元（保留插件数据与备份）=="
for p in $PLUGIN_IDS; do
  systemctl disable --now "$p.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/$p.service"
done
systemctl daemon-reload

echo "== 5. 移除开机钩子 =="
rm -f /etc/syshotplug/pool/97.ssh-persistence
rm -f /data/etc/upper/syshotplug/pool/97.ssh-persistence

echo "== 6. 停用 SSH（设备将失去 root 远程访问）=="
systemctl disable --now minas-sshd.service 2>/dev/null || true
rm -f /etc/systemd/system/minas-sshd.service
systemctl daemon-reload

if [ "$PURGE" = 1 ]; then
  echo "== 7. 清除运行时与入口 =="
  if [ -n "$USER_NAME" ]; then
    for p in $PLUGIN_IDS; do
      rm -f "/data/plugin/www/$USER_NAME/$p" 2>/dev/null || true
    done
  fi
  rm -rf /data/minas /data/minas-ssh
  rm -f /etc/profile.d/keepctl.sh
else
  echo "== 7. 保留 /data/minas 与 /data/minas-ssh（含备份，便于回滚）=="
fi

echo
echo "卸载完成。注意："
echo "  · 已写入 /etc/ld.so.preload 的内容需确认已恢复（原工具会自动恢复）"
echo "  · SMB 临时账号由 root 工具自动清理；如残留请到 App 的共享设置里删除"
echo "  · 设备仍保留 root 能力，SSH 通道已关闭；重新部署请再跑一遍 deploy.ps1"
