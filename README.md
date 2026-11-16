# xiaomi-nas-toolkit

小米智能存储（RP05）的**免 WSL 一键部署工具包**：拿到 root → 固化 SSH → 装插件 → 让插件在重启、对账、厂商清理后依然活着。

面向固件 **1.0.9.464.F24C26D24-REL** 实测（1.0.9.x 系列原理相同，新固件需重新评估）。

---

## ⚠️ 先读这段

**这是给"自己的设备"用的研究工具，不是产品。**

- 会**永久改变**设备状态：关闭原厂签名校验链路、植入 SSH 服务、修改 `/etc`。
- 会**失去保修**（厂商可能以"非原厂固件/配置"拒绝）。设备有**防降级**机制，刷上去退不回旧版本。
- 厂商 OTA 若清空 `/data` 或改动组件协议，本工具装的插件会失效，需要重新部署；**无法承诺任意 OTA 后零影响**。
- 设备被 root 后，局域网内任何拿到你私钥的人都能以 root 身份操作它。请保管好 `~/.ssh/id_ed25519_minas`。
- 请在动手前确认：**法律允许**（多数地区对自有设备做研究性修改是允许的）、**你承担得起砖的风险**。

作者不对数据丢失、保修失效、设备变砖负责。**不提供**降级方案。

---

## 它解决什么问题

原厂固件在这台机器上留了三个坑，本项目逐个处理：

| 坑 | 现象 | 本项目的做法 |
|---|---|---|
| 没有 SSH | 只能通过 App 管理，无法命令行运维 | 用固件漏洞拿到 root，植入修补版 dropbear + 开机自启 + 开机钩子双保险 |
| 插件会被"对账"删掉 | 自研插件运行中突然消失，日志出现 `already taken down` | **目录守卫**：本地代理云端插件目录，把自研插件合并进响应；云端不可达时 fail-closed（宁可不同步，也不删插件） |
| 重启丢插件 | 重启后端口不监听、App 显示"需要下载" | 运行时迁出 `/home`、注册 `ports` 表、自愈定时器修状态位 |

---

## 前提条件

- **Windows 10/11**（主力路径）。不需要 WSL、不需要 Docker、不需要编译工具链。
- **Python 3.10+**（`python --version` 能看到即可）。
- 本机装过「小米智能存储」桌面端**并登录过目标设备**——root 工具要用它的客户端证书（`%LOCALAPPDATA%\minasCert`）。
- NAS 与电脑在同一局域网，能访问 `443`。
- 一个米家账号下的**自有设备**。

> 证书默认有效期约 1 年，过期后需在桌面端重新登录一次以刷新。证书只影响"重新 root"，不影响已建立的 SSH。

---

## 快速开始（小白版）

一共三步，不用改任何配置文件：

1. 确认电脑上装过「小米智能存储」客户端并**登录过**（脚本要用它留下的证书）
2. **双击 `windows\一键部署.cmd`**
3. 等着，看到 🎉 就完成了

脚本会自动：检查环境 → 扫描局域网找到你的 NAS（找不到才会问你 IP）→
生成密钥 → 下载组件 → root → 固化 SSH → 装插件 → 装保护组件 → 自动验收。
全程约 6~10 分钟。中途失败了照着屏幕上的 numbered 建议做，修好重新双击即可——
已完成的步骤会自动跳过，不会重复折腾。

结束后：

```powershell
ssh nas                      # 直接进 root shell（已自动配好别名）
```

> 进阶：分阶段执行、改定时策略、卸载等，见下文和 [docs/04-运维手册.md](docs/04-运维手册.md)。

---

## 分阶段执行（进阶）

一次跑完容易在中间出错时看不出位置，所以每个阶段都能单独跑：

```powershell
powershell -ExecutionPolicy Bypass -File windows\deploy.ps1 -Stage check
```

| 阶段 | 做什么 | 是否改动设备 |
|---|---|---|
| `check` | 环境自检：Python / 证书 / 上游代码 / 配置 / 443 可达 | 否 |
| `probe` | 只读预检 + **写预演**（建一个含 `%0A` 的目录再删掉，验证注入链还在） | 预演写一个目录并删除 |
| `root` | 执行漏洞利用，拿到临时 root shell（2323） | 是（会留下公钥） |
| `ssh` | 修补 dropbear、写 systemd 单元、装开机钩子、注入公钥、可选设密码 | 是 |
| `plugins` | 安装 `dockerctl` / `sysmon` / `pcbackup` | 是 |
| `stack` | 目录守卫 + 自愈定时器 + 运行时迁移，含一次真实对账 | 是 |
| `verify` | 验收（`--reconcile` 再对账一次），写 `~/.ssh/config` 别名 | 只读 + 写本机 SSH 配置 |

**写预演这一步别跳过**：它只花 3 秒，但能在厂商修补漏洞后立刻告诉你"别继续了"，避免在设备上留下无用的临时 SMB 账号。

其他参数：

```powershell
-Stage verify             # 只验收（升级固件后跑这个）
-Stage uninstall          # 卸载（停止并移除全部组件）
-Yes                      # 无人值守，跳过确认
-RootPassword 'xxx'       # 直接指定 root 密码（启用密码登录兜底）
```

---

## 架构

```
                    ┌──────────────────────────────────────────┐
   App 应用市场 ───▶ │ plugincenter（原厂组件，不认识自研插件）   │
                    └───────────────┬──────────────────────────┘
                                    │ POST 目录请求
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │ minas-catalog-guard  127.0.0.1:9310      │
                    │  · 代理云端 server.list                   │
                    │  · 把自研插件合并进响应                    │
                    │  · 云端异常 → 503（fail-closed，绝不删）   │
                    └───────────────┬──────────────────────────┘
                                    │
                                    ▼
                              小米云端 OTA 目录

   keep-plugins.timer ──每 2 分钟/每日 4 次──▶ keep-plugins.py（自愈）
        · 修用户清单状态位（install/status）→ 修掉"需要下载"
        · 补 server.list 与 ports 表
        · 校验单元文件、nginx 路由、2323 防火墙白名单
        · 一切正常则静默退出（约 0.3 秒，零网络请求）
```

| 组件 | 位置 | 作用 |
|---|---|---|
| `catalog_guard.py` | `/data/minas/plugins/catalog/` | 目录守卫，插件不被对账删除的**根因解法** |
| `keep_plugins_v4.py` | `/data/minas-ssh/keep-plugins/keep-plugins.py` | 自愈巡检，修状态位 / 补注册 / 拉起服务 |
| `keepctl.sh` | 同目录，`keepctl` 别名 | 不改 systemd 文件就能调调度、开关自愈 |
| `hook_v4.sh` | `/etc/syshotplug/pool/97.ssh-persistence` | 存储池挂载时**只补不重启**拉起服务 |
| 插件运行时 | `/data/minas/plugins/<id>` | 脱离厂商可重建的 `/home/<uid>/plugin` |

为什么是"只补不重启"、为什么守卫要 fail-closed、为什么定时器不能用 `cron`——见 [docs/02-关键决策.md](docs/02-关键决策.md)。

---

## 部署完成后

设备上新增的东西：

```
/data/minas/                     插件稳定运行时 + 目录守卫
/data/minas-ssh/dropbear         修补版 dropbear（登录 shell 改为 /bin/bash）
/data/minas-ssh/keep-plugins/    自愈脚本、配置、条目缓存
/data/minas-ssh/backup-v4-*/     每次部署前的自动备份
/etc/systemd/system/minas-sshd.service
/etc/systemd/system/minas-catalog-guard.service
/etc/systemd/system/keep-plugins.service|timer
/etc/syshotplug/pool/97.ssh-persistence   开机钩子（/data 侧还有一份镜像）
```

随手可用的命令：

```bash
keepctl status        # 间隔/日历模式、启用状态、下次触发
keepctl set 5min      # 改间隔
keepctl at 08,12,15,18:00   # 改成每天 4 个时间点
keepctl off / on      # 停用 / 恢复自愈
keepctl run           # 立刻巡检一次
keepctl apply         # 手改 keep.conf 后让它生效
```

完整说明见 [docs/04-运维手册.md](docs/04-运维手册.md)。

---

## 常见问题

**Q：重启会掉吗？**
不掉。6 个单元全部 `enabled` 开机自启，外加存储池挂载钩子兜底。已做多次受控重启演练：SSH 约 60 秒恢复，插件健康检查 200，`install` 状态保持。

**Q：会影响硬盘休眠吗？**
不会。自愈只读写 `/data`（eMMC），机械盘只挂载在 `/nas/mnt/pa0|pa1`，自愈流程不碰。`hdidle` 只统计机械盘 IO。

**Q：定时器每 2 分钟跑一次，负担重吗？**
每轮约 0.3 秒、零网络请求、一切正常时不写盘。觉得吵可以 `keepctl set 10min`，代价是被破坏后修复变慢。

**Q：改了定时策略要重启吗？**
不用，`keepctl` 会 `daemon-reload` 并重启 timer，立即生效且重启后保持。

**Q：为什么 SSH 不放在 2222 之类的非常规端口？**
用 22 是为了 `ssh nas` 这种零参数体验；风险由"仅密钥 + 可选密码 + 局域网"控制。2323 的 root shell 则严格限制来源 IP（白名单，默认同网段）。

**Q：`scp` 能用吗？**
能。修补版 dropbear 的服务端配合标准 `scp` 可正常传输（本项目的部署流程本身就靠它）。若你的 OpenSSH 版本较新报 SFTP 错误，用 `scp -O` 走旧协议。

**Q：Python 3.13 能跑吗？**
能。Python 3.13 起 OpenSSL 3.5 默认开启 `X509_STRICT`，而厂商 CA 证书的 `basicConstraints` 没标 `critical`，直接跑上游工具会报 `Basic Constraints of CA cert not marked critical`。本项目的 `nowsl_run.py` 已内置兼容（只撤掉严格位，CA 链与主机名校验保留），无需降级 Python。详见 [docs/04-运维手册.md](docs/04-运维手册.md#python-313--openssl-35-的证书兼容问题)。

---

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File windows\deploy.ps1 -Stage uninstall
```

默认保留 `/data/minas*` 下的备份方便回滚；加 `--purge` 彻底清除（需在 NAS 上手工执行 `sh /tmp/uninstall.sh --purge`）。

注意：卸载会关闭 SSH，**之后只能靠重跑部署或拆机**恢复远程访问。别在没把握的时候卸。

---

## 目录结构

```
config/config.example.json   配置模板（一键脚本会自动生成 config.json，无需手改）
windows/deploy.ps1           主流程（向导式一键 + 分阶段进阶用法）
windows/一键部署.cmd          双击入口
core/find_nas.py             局域网自动发现 NAS（证书指纹 + 443 扫描）
core/nowsl_run.py            无 WSL 通道（socket 直连 2323，运行时打补丁）
core/write_probe.py          写预演（MKCOL 控制字符目录名）
core/ssh_persist.sh          SSH 固化
core/deploy_v4.sh            稳定性栈部署
core/validate_v4.sh          验收
core/keep_plugins_v4.py      自愈巡检
core/catalog_guard.py        目录守卫
core/keepctl.sh + keep.conf  调度管理
core/units/                  systemd 单元模板
tests/                       离线单测（不碰真机）
docs/                        原理、决策、复盘、运维、风险
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/01-工作原理.md](docs/01-工作原理.md) | root 七环链路、SSH 修补原理、目录守卫为什么能拦住删除 |
| [docs/02-关键决策.md](docs/02-关键决策.md) | 选型对比、6 个设计冲突的取舍理由 |
| [docs/03-故障复盘.md](docs/03-故障复盘.md) | 三次真实事故的根因与修法 |
| [docs/04-运维手册.md](docs/04-运维手册.md) | 日常操作、`keepctl` 全参数、排障阶梯 |
| [docs/05-风险与边界.md](docs/05-风险与边界.md) | 能力边界、OTA 影响矩阵、不可逆操作清单 |

---

## 致谢

本项目是**编排层**，不分发第三方代码与二进制，运行时按需拉取：

- [lvmaple/minas_plugin](https://github.com/lvmaple/minas_plugin) —— 插件源码、统一部署工具、1.0.9.x 的 SSH 开启工具
- [xuchen96/xiaomi-nas-guide](https://github.com/xuchen96/xiaomi-nas-guide) —— SSH 持久化的 systemd 单元与开机钩子思路

本仓库原创部分：参数化配置、一键编排、插件运行时迁移、目录守卫、自愈与调度管理。

MIT License，见 [LICENSE](LICENSE)。
