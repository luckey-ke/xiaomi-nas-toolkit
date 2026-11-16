#!/bin/sh
# MINAS 开机钩子（v4）。由 syshotplug 在存储池挂载时调用。
#
# 三条规则，每条都来自一次真实故障：
#  1. 只补不重启：绝不重启正在运行的 minas-sshd——早期版本用 restart，
#     导致存储池重挂载时把活跃 SSH 会话一并踢掉。
#  2. 先起目录守卫再起自愈，保证 plugincenter 对账时看到的是合并后的目录。
#  3. 一次 daemon-reload，所有 systemctl 用 --no-block，避免挂载流程被阻塞。
#
# 本脚本不写死任何插件名：/tmp 下的日志目录由自愈脚本按 plugins.json 建立。
if [ "$ACTION" = "mounted" ]; then
  # dropbear 以 root 登录时要求 home 存在；固件默认 /root 不存在，
  # 改成 /home/rootx 让它能正常起 shell。
  if grep -q '^root:x:0:0:root:/root:' /etc/passwd 2>/dev/null; then
    sed -i 's#^root:x:0:0:root:/root:#root:x:0:0:root:/home/rootx:#' /etc/passwd
  fi
  systemctl daemon-reload 2>/dev/null

  systemctl --no-block enable minas-sshd.service 2>/dev/null
  systemctl is-active --quiet minas-sshd.service 2>/dev/null \
    || systemctl --no-block start minas-sshd.service 2>/dev/null

  systemctl --no-block enable minas-catalog-guard.service 2>/dev/null
  systemctl is-active --quiet minas-catalog-guard.service 2>/dev/null \
    || systemctl --no-block start minas-catalog-guard.service 2>/dev/null

  systemctl --no-block enable keep-plugins.timer 2>/dev/null
  systemctl is-active --quiet keep-plugins.timer 2>/dev/null \
    || systemctl --no-block start keep-plugins.timer 2>/dev/null

  systemctl is-active --quiet keep-plugins.service 2>/dev/null \
    || systemctl --no-block start keep-plugins.service 2>/dev/null

  logger -t ssh.persistence "hook fired (v4: ssh/catalog/self-heal armed)"
fi
