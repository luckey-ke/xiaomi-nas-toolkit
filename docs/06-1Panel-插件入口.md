# 1Panel 插件入口（应用市场内嵌）

小米智能存储的「应用市场」只展示在本机插件目录注册过的条目（INFO + UI + 图标 + 清单条目）。
1Panel 是宿主服务（systemd + `/data/1panel`），本身不是插件，因此默认不会出现在市场里。

本目录是给 1Panel 做的**轻量外壳插件**：市场里显示图标与「打开」，点开后用 iframe 内嵌
`http://<NAS>:18080/index`。1Panel 本体不参与打包，面板内升级不受影响。

## 运行方式

| 组件 | 路径（设备上） | 作用 |
|---|---|---|
| 插件包 | `/data/minas/plugins/1panel/` | `INFO` 元数据、`src/ui` 页面、`scripts/control` 启停 |
| 静态服务 | `src/files/httpd.py` | 监听 `0.0.0.0:9304`，只提供 `index.html` 与 `/health`（探测 18080） |
| 图标 | `/data/plugin/www/icon/1panel.icon` | 300×300 PNG，绿色圆角 `1P` |
| 清单条目 | `keep-plugins/entries.json` → `1panel` | 自愈据此恢复 `server.list` 与用户 `.list` 的安装态 |
| 目录条目 | `/data/minas/plugins/catalog/entries.json` | 由自愈从上面的条目裁剪生成（仅 info + resource） |
| 备份 | `/data/backup/plugins/1panel/` | 包副本 + `list-entry.json` + `restore.sh`，重启/OTA 后自愈 |
| 开机 | `/etc/syshotplug/pool/96.1panel-overlay` | 挂 `/usr` overlay、起 1panel-core/agent 与 `1panel.service` |

## 为什么不让 1Panel 跑在 Docker 里

官方明确不支持面板容器化（1Panel-dev/1Panel#1981）：面板要管理宿主 Docker、写防火墙、
注册 systemd 服务与计划任务、按宿主路径做备份，容器内会大面积失真。社区镜像只适合
"宿主机装不了"的系统（群晖、openwrt）。本机根分区虽为 erofs 只读，但用 `/usr` overlay
后官方脚本可原样运行，宿主安装是功能最完整的路径。

## 重建 / 手工恢复

```sh
# 1) 同步插件包
scp -r INFO scripts src nas:/data/minas/plugins/1panel/
# 2) 同步条目缓存（真源）
#    把 list-entry.json 的 1panel 条目并入 /data/minas-ssh/keep-plugins/entries.json
# 3) 跑一次自愈
ssh nas 'cd /data/minas-ssh/keep-plugins && python3 keep-plugins.py'
# 4) 验证
ssh nas 'systemctl is-active 1panel; curl -s http://127.0.0.1:9304/health'
```

> 端口固定 9304（已登记在 `plugins.json` 的 ports 表，供 nginx 路由生成使用）。
