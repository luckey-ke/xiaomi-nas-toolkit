#!/bin/sh
set -e
# 1Panel 市场入口插件开机自愈：系统重启会清掉手动插件目录与 .list，从 /data 备份恢复。
# UID 不写死：从自愈配置读取，缺失时用环境变量 MINAS_UID 兜底。
UID_N="${MINAS_UID:-$(python3 -c 'import json;print(json.load(open("/data/minas-ssh/keep-plugins/plugins.json"))["uid"])' 2>/dev/null)}"
[ -n "${UID_N}" ] || { echo "restore: 无法确定 UID，退出"; exit 1; }
PLUGIN=1panel
SERVICE=1panel
PORT=9304
USER_NAME=u${UID_N}
HOME_DIR=/home/${USER_NAME}/plugin/${PLUGIN}
WWW_DIR=/data/plugin/www/${USER_NAME}
LIST_FILE=/data/plugin/${USER_NAME}.list
BACKUP_DIR=/data/backup/plugins/${PLUGIN}
SRC=${BACKUP_DIR}/${PLUGIN}

# 1) 插件目录（存在则跳过）
if [ ! -f "${HOME_DIR}/INFO" ] && [ -f "${SRC}/INFO" ]; then
    mkdir -p /home/${USER_NAME}/plugin
    cp -a "${SRC}" "${HOME_DIR}"
    chmod +x "${HOME_DIR}/scripts/control" "${HOME_DIR}/src/files/httpd.py" 2>/dev/null || true
    chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}" 2>/dev/null || true
    echo "restore: 插件目录已恢复"
fi

# 2) www 软链 + 图标（指向稳定运行时）
mkdir -p "${WWW_DIR}" /data/plugin/www/icon
ln -sfn "/data/minas/plugins/${PLUGIN}/src/ui" "${WWW_DIR}/${PLUGIN}"
cp "/data/minas/plugins/${PLUGIN}/src/ui/icon.png" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true
chown "${USER_NAME}:${USER_NAME}" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true

# 3) 合并 .list（用备份的注册条目，保留已安装状态）
if [ -f "${BACKUP_DIR}/list-entry.json" ]; then
    python3 - "${LIST_FILE}" "${BACKUP_DIR}/list-entry.json" <<'PY2'
import json, sys, os
list_path, entry_path = sys.argv[1], sys.argv[2]
entry = json.load(open(entry_path))
data = {}
if os.path.isfile(list_path):
    try:
        with open(list_path) as f:
            data = json.load(f)
    except ValueError:
        data = {}
data.update(entry)
with open(list_path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print("restore: .list 已合并")
PY2
fi

# 4) systemd unit + 启动服务
if [ -f "${HOME_DIR}/scripts/control" ]; then
    PLUG_SKIP_SERVICE_START=1 PLUG_SRC_DIR="${HOME_DIR}/src" PLUG_HOME_DIR="${HOME_DIR}" \
      PLUG_USER="${USER_NAME}" PLUG_PORT_ALLOC="${PORT}" sh "${HOME_DIR}/scripts/control" postinstall
fi
systemctl --no-block start "${SERVICE}.service"
exit 0
