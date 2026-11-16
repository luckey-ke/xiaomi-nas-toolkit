#!/bin/sh
# 在 NAS 上固化 SSH：修补 dropbear + 写 systemd 单元 + 装开机钩子。
# 以 root 运行（通过 2323 root shell 或已有 SSH）。
#
# 可选输入（两者都通过文件传递，避免出现在命令行参数里）：
#   /tmp/minas_authorized_keys   要追加到 /etc/dropbear/authorized_keys 的公钥
#   MINAS_ROOT_PASSWORD          环境变量；设置后启用密码登录并写入 root 密码
#
# 说明：固件自带的 dropbear 把登录 shell 硬编码为 /usr/sbin/mi-shell（一个
# 受限菜单），这里就地替换为 /bin/bash。替换必须严格等长（18 字节），否则
# 会破坏 ELF 内的偏移，因此用 /bin/bash + 9 个 NUL 填充。
set -eu
export PATH="$PATH:/usr/sbin:/sbin"

PATCHED=/data/minas-ssh/dropbear
UNIT=/etc/systemd/system/minas-sshd.service
HOOK=/etc/syshotplug/pool/97.ssh-persistence
HOOK_MIRROR=/data/etc/upper/syshotplug/pool/97.ssh-persistence

echo "=== [1/6] 修补 dropbear ==="
mkdir -p /data/minas-ssh
python3 - <<'PY'
src = "/usr/sbin/dropbear"
dst = "/data/minas-ssh/dropbear"
data = open(src, "rb").read()
needle = b"/usr/sbin/mi-shell"
repl = b"/bin/bash" + b"\x00" * 9
count = data.count(needle)
print("mi-shell 出现次数:", count)
if count != 1:
    raise SystemExit("中止：出现次数异常，未写入补丁文件")
open(dst, "wb").write(data.replace(needle, repl))
print("已写入补丁副本，字节数:", len(data))
PY
chmod 755 "$PATCHED"
strings "$PATCHED" | grep -m1 '/bin/bash' >/dev/null && echo "补丁校验通过"

echo "=== [2/6] 准备 host key 与 authorized_keys ==="
if [ ! -f /etc/dropbear/dropbear_rsa_host_key ]; then
  mkdir -p /etc/dropbear
  if command -v dropbearkey >/dev/null 2>&1; then
    dropbearkey -t rsa -f /etc/dropbear/dropbear_rsa_host_key >/dev/null
    echo "已生成 RSA host key"
  else
    HOST_KEY_FLAG="-R"
    echo "无 dropbearkey，改为启动时自动生成（-R）"
  fi
fi
: "${HOST_KEY_FLAG:=}"
if [ -f /tmp/minas_authorized_keys ]; then
  mkdir -p /etc/dropbear
  touch /etc/dropbear/authorized_keys
  chmod 600 /etc/dropbear/authorized_keys
  # 去重追加：同一把公钥重复部署不会堆积
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    grep -qF "$line" /etc/dropbear/authorized_keys || printf '%s\n' "$line" >> /etc/dropbear/authorized_keys
  done < /tmp/minas_authorized_keys
  rm -f /tmp/minas_authorized_keys
  echo "已注入公钥，当前 authorized_keys 行数：$(wc -l < /etc/dropbear/authorized_keys)"
else
  echo "未提供公钥文件（仍可用密码登录）"
fi

echo "=== [3/6] 处理 root 密码 ==="
if [ -n "${MINAS_ROOT_PASSWORD:-}" ]; then
  printf 'root:%s\n' "$MINAS_ROOT_PASSWORD" | chpasswd
  AUTH_FLAG=""
  echo "已设置 root 密码，密码登录开启"
else
  AUTH_FLAG="-s"
  echo "未设置密码，仅允许密钥登录（-s）"
fi

echo "=== [4/6] 写 systemd 单元 ==="
cat > "$UNIT" <<UNIT
[Unit]
Description=MINAS patched dropbear sshd
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/data/minas-ssh/dropbear
Conflicts=dropbear.socket

[Service]
# -s 表示禁用密码认证；有密码时需要去掉。$HOST_KEY_FLAG 仅在缺 host key 时出现。
ExecStart=$PATCHED -F -p 22 -r /etc/dropbear/dropbear_rsa_host_key -K 60 $AUTH_FLAG $HOST_KEY_FLAG
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$UNIT"
# 清掉可能的双空格
sed -i 's/  */ /g; s/ $//' "$UNIT"

echo "=== [5/6] 写开机钩子（双份）==="
mkdir -p /etc/syshotplug/pool /data/etc/upper/syshotplug/pool
install -m 755 /data/minas/plugins/catalog/stage/hook_v4.sh "$HOOK" 2>/dev/null || true
if [ ! -f "$HOOK" ]; then
  cat > "$HOOK" <<'HOOKSRC'
#!/bin/sh
# MINAS 开机钩子（v4）
# 规则：只补不重启——绝不重启正在运行的 sshd，避免存储池挂载事件踢掉活跃会话。
if [ "$ACTION" = "mounted" ]; then
  sed -i 's#^root:x:0:0:root:/root:#root:x:0:0:root:/home/rootx:#' /etc/passwd 2>/dev/null
  mkdir -p /tmp/dockerctl /tmp/sysmon /tmp/pcbackup
  systemctl daemon-reload 2>/dev/null
  systemctl --no-block enable minas-sshd.service 2>/dev/null
  systemctl is-active --quiet minas-sshd.service 2>/dev/null || systemctl --no-block start minas-sshd.service 2>/dev/null
  systemctl --no-block enable minas-catalog-guard.service 2>/dev/null
  systemctl is-active --quiet minas-catalog-guard.service 2>/dev/null || systemctl --no-block start minas-catalog-guard.service 2>/dev/null
  systemctl --no-block enable keep-plugins.timer 2>/dev/null
  systemctl is-active --quiet keep-plugins.timer 2>/dev/null || systemctl --no-block start keep-plugins.timer 2>/dev/null
  logger -t ssh.persistence "hook fired (v4)"
fi
HOOKSRC
  chmod 755 "$HOOK"
fi
cp "$HOOK" "$HOOK_MIRROR"
chmod 755 "$HOOK_MIRROR"

echo "=== [6/6] 激活 ==="
systemctl daemon-reload
systemctl disable dropbear.socket 2>/dev/null || true
systemctl --no-block stop dropbear.socket 2>/dev/null || true
systemctl enable minas-sshd.service 2>&1
systemctl --no-block restart minas-sshd.service 2>&1
sleep 4
echo "--- is-active ---"; systemctl is-active minas-sshd.service 2>&1
echo "--- is-enabled ---"; systemctl is-enabled minas-sshd.service 2>&1
echo "--- listening ---"; ss -tln 2>/dev/null | grep ':22 ' || true
echo "=== SSH 固化脚本完成 ==="
