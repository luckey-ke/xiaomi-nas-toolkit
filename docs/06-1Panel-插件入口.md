# 1Panel 插件入口（应用市场内嵌）

小米智能存储的「应用市场」只展示在本机插件目录注册过的条目（INFO + UI + 图标 + 清单条目）。
1Panel 是宿主服务（systemd + `/data/1panel`），本身不是插件，因此默认不会出现在市场里。
本目录是给 1Panel 做的**入口插件**：市场里显示图标，点开后直接在应用窗口里打开面板本体。

## 为什么不 iframe、不跳转

客户端把插件页加载在本机 https 代理里（形如 `https://127.0.0.1:<port>/D:/plugin/<uid>/1panel/index.html`），
实测行为：

| 方式 | 结果 |
|---|---|
| `iframe src=...` | 请求**根本不发出**（nginx 日志里一条都没有），窗口一片空白 |
| `location.replace()` 跳转 | 被应用层拦截 |
| 同源 `fetch` / `<script>` / `<link>` | 正常 |

所以唯一可行的路子：**插件入口页本身就是 1Panel 的 SPA**。`mirror_assets.py` 把面板前端整份
镜像到插件的 `src/ui`（实测 729 个文件、33MB、697 个 JS 分片），并把下面三处改写成插件前缀：

- `history:xs(\`/\`)` → `history:xs(\`/plugin/981760641/1panel/\`)`（router base）
- `baseURL:\`/api/v2\`` → `baseURL:\`/plugin/981760641/1panel/api/v2\``
- 资源引用 `/assets/...` → `/plugin/981760641/1panel/assets/...`

## 最关键的一层：服务端 cookie 罐

1Panel 的登录态（`psession`）、安全入口、公钥全靠 cookie。而客户端的 webview
**不保存任何 cookie**：

- 客户端 profile 的 Cookies 库 `0` 行（`SmartStorage/Network/Cookies`）
- 页面里 `document.cookie` 长度为 `0`

于是“密码正确也会立刻提示当前会话已过期”。解法是在插件后端 `src/files/httpd.py` 做一层
API 桥（`0.0.0.0:9304`，nginx 把 `…/1panel/api/` 转发过来）：

| 问题 | 桥的做法 |
|---|---|
| 浏览器没有 `psession` | 桥自己维护 cookie 罐，浏览器侧零 cookie；用户登录一次后所有请求由桥补 `Cookie` |
| 非 GET 请求被 CSRF 拦（`CSRF token invalid`） | 桥把罐里的 `pcsrftoken` 填进 `X-CSRF-Token` |
| 登录密码要 `rsa:iv:aes` 三段式 | 前端已内置公钥；万一发来明文，桥用罐里最新的公钥重新加密（`cryptography`） |
| WebSocket（终端、日志流）需要 cookie | 桥做裸 socket 升级 + 双向搬运，同样补 Cookie |

实测：客户端零 cookie 的情况下，登录 → `auth/current` → `settings/search/base` → 面板首页各接口全部 200。

> 安全边界：1Panel 本体本来就监听 `0.0.0.0:18080`，局域网可直接访问，共享会话罐不额外
> 扩大暴露面。真正的防线是**不要把 NAS 暴露到公网**。

## 运行方式

| 组件 | 路径（设备上） | 作用 |
|---|---|---|
| 插件包 | `/data/minas/plugins/1panel/` | `INFO`、`src/ui`（镜像后的面板前端）、`scripts/control` 启停 |
| 服务 | `src/files/httpd.py` | `0.0.0.0:9304`：静态托管 `src/ui` + `/api/` 反代 + `/health` |
| 图标 | `/data/plugin/www/icon/1panel.icon` | 300×300 PNG，绿色圆角 `1P` |
| 清单条目 | `keep-plugins/entries.json` → `1panel` | 自愈据此恢复 `server.list` 与用户 `.list` 的安装态 |
| 目录条目 | `/data/minas/plugins/catalog/entries.json` | 由自愈从上面的条目裁剪生成（仅 info + resource） |
| 备份 | `/data/backup/plugins/1panel/` | 包副本 + `list-entry.json` + `restore.sh`，重启/OTA 后自愈 |
| 开机 | `/etc/syshotplug/pool/96.1panel-overlay` | 挂 `/usr` overlay、起 1panel-core/agent 与 `1panel.service` |

nginx 侧需要两样东西：

1. `conf.d/luci/1panel-panel.conf`（本目录同名文件）——把 `/plugin/<uid>/1panel/{assets,api,ws,health}`
   路由到本地文件或 9304，并统一剥掉客户端可能带的 `/D:/` 盘符前缀。
2. `sites-enabled/luci.conf` 里给 `$need_reject` 加一条 token 豁免，否则静态资源会被网关拒：

```nginx
~^/plugin/u?981760641/1panel/ 0;
~^/D:/plugin/u?981760641/1panel/ 0;
```

## 面板升级后必须重跑

镜像里的分片和公钥都来自当前版本，面板升级后要重新生成一次：

```sh
ssh nas 'cd /data/minas/plugins/1panel && python3 mirror_assets.py'
ssh nas 'systemctl restart 1panel.service'
```

脚本会打印抓取数量；如果只有几十个文件，说明面板前端产物路径变了，
需要先确认 `mirror_assets.py` 顶部的入口路径。

## 已知限制

- **终端 / 容器日志流**：走 WebSocket，桥已做升级转发，但 1Panel 的 ws 端点参数（`operateNode`）
  依赖前端运行时状态，仍在验证中；打不开时不影响面板其它功能。
- 面板前端整份静态镜像（729 个文件），面板升级后必须重跑镜像脚本，否则新版本页面会缺分片。

## 重建 / 手工恢复

```sh
# 1) 同步插件包
scp -r INFO scripts src mirror_assets.py nginx-1panel-panel.conf nas:/data/minas/plugins/1panel/
# 2) 生成前端镜像
ssh nas 'cd /data/minas/plugins/1panel && python3 mirror_assets.py'
# 3) 同步条目缓存（真源）后跑一次自愈
ssh nas 'cd /data/minas-ssh/keep-plugins && python3 keep-plugins.py'
# 4) 验证：入口页、API、登录态
ssh nas 'curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/plugin/981760641/1panel/index.html'
ssh nas 'curl -sk https://127.0.0.1/plugin/981760641/1panel/api/v2/core/auth/current'
```

> 端口固定 9304（已登记在 `plugins.json` 的 ports 表，供 nginx 路由生成使用）。
