#requires -Version 5.1
<#
.SYNOPSIS
    小米智能存储（RP05）一键部署工具 —— 双击即用版。

.DESCRIPTION
    给小白：直接双击「一键部署.cmd」，全程跟着提示走即可。
    脚本会自动完成：找证书 → 找 NAS → 生成密钥 → 下载组件 →
    root → 固化 SSH → 装插件 → 装保护组件 → 自动验收。

    中断后再跑：已完成的步骤会自动跳过（进度存在 config\.state.json）。
    想从头再来：加 -Force。

    给进阶用户（详见 docs/04-运维手册.md）：
      -Stage check      只做环境自检
      -Stage verify     固件升级后复检
      -Stage uninstall  卸载全部组件
      -Stage root / ssh / plugins / stack / probe   单独跑某一步

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\deploy.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'check', 'probe', 'root', 'ssh', 'plugins', 'stack', 'verify', 'uninstall')]
    [string]$Stage = 'auto',
    [switch]$Force,      # 忽略进度标记，全部重跑
    [switch]$Yes,        # 跳过确认提问（无人值守）
    [string]$RootPassword  # 预设 root 密码；不传则在 SSH 阶段询问（可留空）
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # 关掉下载/解压的进度条刷屏

# ============================================================
# 路径常量（脚本位置无关，全部相对仓库根目录推导）
# ============================================================
$script:Root      = Split-Path -Parent $PSScriptRoot          # 仓库根目录
$script:Core      = Join-Path $script:Root 'core'             # 核心脚本（Python/Shell）
$script:Vendor    = Join-Path $script:Root 'vendor\upstream'  # 上游代码（自动下载）
$script:ConfigDir = Join-Path $script:Root 'config'
$script:ConfigPath = Join-Path $script:ConfigDir 'config.json'  # 你的配置（自动生成，不入库）
$script:StatePath  = Join-Path $script:ConfigDir '.state.json'  # 进度标记（断点续跑用）
$script:StageDir   = '/data/minas/plugins/catalog/stage'        # NAS 上的部署暂存目录
$script:Python     = $null   # 由「找-Python」解析，不用裸 python（避开商店占位 stub）

# ============================================================
# 找一个真正能用的 Python：
#   依次尝试 python → py 启动器 → 常见安装目录（含 WorkBuddy 自带）。
#   注意：Windows 自带的 python.exe 可能是「打开应用商店」的占位 stub，
#   所以必须真的跑一下 -c 验证输出，而不是只看命令存在。
# ============================================================
function 找-Python {
    $候选 = @('python')
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $候选 += 'py' }
    foreach ($模式 in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                        "$env:USERPROFILE\.workbuddy\binaries\python\versions\*\python.exe",
                        'C:\Python3*\python.exe')) {
        $命中 = Get-Item $模式 -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if ($命中) { $候选 += $命中.FullName }
    }
    foreach ($c in $候选) {
        try {
            $ver = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^\d+\.\d+$') {
                return @{ 路径 = $c; 版本 = $ver.Trim() }
            }
        } catch { }
    }
    return $null
}

# ============================================================
# 日志输出：四种级别，统一格式
#   ✓ 成功（绿）  → 进行中（灰）  ! 警告（黄）  ✗ 失败（红）
# ============================================================
function 说-步骤($文本) { Write-Host ''; Write-Host "【$文本】" -ForegroundColor Cyan }
function 说-成功($文本) { Write-Host "  ✓ $文本" -ForegroundColor Green }
function 说-信息($文本) { Write-Host "  → $文本" -ForegroundColor Gray }
function 说-警告($文本) { Write-Host "  ! $文本" -ForegroundColor Yellow }
function 说-失败($文本) { Write-Host "  ✗ $文本" -ForegroundColor Red }

# 失败即停：打印「现象 +  numbered 排查建议」，不让小白面对堆栈
function 停在这里($现象, [string[]]$建议) {
    Write-Host ''
    说-失败 $现象
    if ($建议) {
        Write-Host '  你可以这样排查：' -ForegroundColor Yellow
        for ($i = 0; $i -lt $建议.Count; $i++) {
            Write-Host ("    {0}. {1}" -f ($i + 1), $建议[$i]) -ForegroundColor Yellow
        }
    }
    Write-Host '  修好后重新双击「一键部署.cmd」即可，已完成的步骤会自动跳过。' -ForegroundColor DarkGray
    exit 1
}

function 询问($问题, $默认 = '') {
    if ($默认) { $提示 = "$问题（回车=$默认）" } else { $提示 = $问题 }
    $答 = Read-Host "  $提示"
    if ([string]::IsNullOrWhiteSpace($答)) { return $默认 }
    return $答.Trim()
}

# ============================================================
# 进度标记：每完成一步写进 config\.state.json，重跑时自动跳过
# ============================================================
function 读-进度 {
    if (Test-Path $script:StatePath) {
        try { return @((Get-Content $script:StatePath -Raw -Encoding UTF8 | ConvertFrom-Json)) }
        catch { return @() }
    }
    return @()
}
function Test-完成($步骤) {
    if ($Force) { return $false }
    return (读-进度) -contains $步骤
}
function 标记-完成($步骤) {
    $进度 = @(读-进度)
    if ($进度 -notcontains $步骤) {
        $进度 += $步骤
        if (-not (Test-Path $script:ConfigDir)) { New-Item -ItemType Directory -Force -Path $script:ConfigDir | Out-Null }
        [System.IO.File]::WriteAllText($script:StatePath,
            ($进度 | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    }
}

# ============================================================
# 配置读写
# ============================================================
function 读-配置 {
    if (-not (Test-Path $script:ConfigPath)) { return $null }
    return Get-Content $script:ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
function 写-配置($配置) {
    if (-not (Test-Path $script:ConfigDir)) { New-Item -ItemType Directory -Force -Path $script:ConfigDir | Out-Null }
    $json = $配置 | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($script:ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ============================================================
# 子进程封装：统一错误处理
# ============================================================
function 跑-Python {
    param([string[]]$参数, [switch]$允许失败, [switch]$静默)
    $输出 = & $script:Python -X utf8 @参数 2>&1
    $码 = $LASTEXITCODE
    if (-not $静默) { $输出 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    if ($码 -ne 0 -and -not $允许失败) {
        停在这里 "Python 脚本执行失败（退出码 $码）" @('看上面最后一行输出定位原因', '重新双击一键部署重试')
    }
    return $输出
}

function 跑-SSH {
    param([string]$命令, [switch]$允许失败, [switch]$静默)
    $cfg = 读-配置
    $sshArgs = @(
        '-i', $cfg.access.key_path,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=10',
        "$($cfg.access.ssh_user)@$($cfg.device.host)",
        $命令
    )
    $输出 = & ssh @sshArgs 2>&1
    $码 = $LASTEXITCODE
    if (-not $静默) { $输出 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    if ($码 -ne 0 -and -not $允许失败) {
        停在这里 "NAS 上的命令执行失败（退出码 $码）" @('确认能登录：ssh nas', '重新双击一键部署重试')
    }
    return @{ 码 = $码; 输出 = $输出 }
}

function 传-文件($本地, $远端) {
    $cfg = 读-配置
    $目录 = ($远端 -replace '/[^/]+$', '')
    跑-SSH "mkdir -p '$目录'" -静默 | Out-Null
    $scpArgs = @('-i', $cfg.access.key_path, '-o', 'StrictHostKeyChecking=accept-new',
                 $本地, "$($cfg.access.ssh_user)@$($cfg.device.host):$远端")
    $out = & scp @scpArgs 2>&1
    if ($LASTEXITCODE -ne 0) { 停在这里 "上传文件失败：$本地" @($out -join ' ') }
}

function 测-端口($主机名, $端口, $毫秒 = 1500) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $t = $c.ConnectAsync($主机名, $端口)
        return ($t.Wait($毫秒) -and $c.Connected)
    } catch { return $false } finally { $c.Close() }
}

function 等-端口($主机名, $端口, $秒, $名字) {
    $截止 = (Get-Date).AddSeconds($秒)
    while ((Get-Date) -lt $截止) {
        if (测-端口 $主机名 $端口 1500) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# SSH 免密是否已可用（用于判断"这台机器是不是已经部署过了"）
function 测-SSH可用 {
    $cfg = 读-配置
    if (-not $cfg) { return $false }
    if (-not (Test-Path $cfg.access.key_path)) { return $false }
    $r = 跑-SSH 'id -u' -允许失败 -静默
    return ($r.码 -eq 0 -and ($r.输出 -join '') -match '^\s*0')
}

# ============================================================
# 第 1 步：环境自检（Python / 证书 / 密钥 / 上游代码）
# ============================================================
function 步骤-环境自检 {
    说-步骤 '第 1 步 / 共 6 步：检查你的电脑环境'

    # Python 是全部工具的载体，没有就直接给安装指引
    $py = 找-Python
    if (-not $py) {
        停在这里 '没有找到 Python' @(
            '打开 https://www.python.org/downloads/ 下载安装',
            '安装时务必勾选「Add python.exe to PATH」',
            '装完后重新双击一键部署')
    }
    $script:Python = $py.路径
    $major, $minor = $py.版本.Split('.')
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
        停在这里 "Python 版本太低（$($py.版本)）" @('需要 3.10 或更新版本，去 python.org 下载最新版')
    }
    说-成功 "Python $($py.版本)"

    # keystone-engine：root 工具内置机器码，正常用不到；装不上只警告
    & $script:Python -c "import keystone" 2>$null
    if ($LASTEXITCODE -ne 0) {
        说-信息 '安装可选组件 keystone-engine'
        & $script:Python -m pip install keystone-engine --quiet --disable-pip-version-check 2>$null
    }
    说-成功 '组件依赖就绪'

    # 证书：小米存储 App 登录后自动留在 %LOCALAPPDATA%\minasCert
    $证书目录 = Join-Path $env:LOCALAPPDATA 'minasCert'
    $证书 = @()
    if (Test-Path $证书目录) { $证书 = @(Get-ChildItem $证书目录 -Filter '*_cert.pem') }
    if ($证书.Count -eq 0) {
        停在这里 '没有找到小米存储的登录证书' @(
            '先在电脑上安装「小米智能存储」客户端（mi.com 官网下载）',
            '打开客户端并登录你的米家账号，确认能看到你的 NAS',
            '然后重新双击一键部署')
    }
    说-成功 "找到登录证书（$($证书[0].Name)）"

    # SSH 密钥：没有就自动生成一对（免密登录用）
    $密钥 = Join-Path $env:USERPROFILE '.ssh\id_ed25519_minas'
    if (-not (Test-Path $密钥)) {
        说-信息 '生成 SSH 密钥（相当于给你配一把专用钥匙）'
        & ssh-keygen -t ed25519 -f $密钥 -N '""' -q 2>$null
        if (-not (Test-Path $密钥)) { 停在这里 '密钥生成失败' @('以管理员身份打开 PowerShell 再试') }
    }
    说-成功 "SSH 密钥就绪"

    # 上游代码：公开仓库不内嵌第三方代码，按需下载（优先 git，无 git 用 zip）
    if (-not (Test-Path (Join-Path $script:Vendor 'deploy-plugins.py'))) {
        说-信息 '下载所需组件（首次运行需要，约 1-2 分钟）'
        下载-上游代码
    }
    说-成功 '组件代码就绪'

    标记-完成 'env'
}

# 下载上游代码：有 git 用 git clone，没有就下载 zip 包解压
function 下载-上游代码 {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $仓库 = 'https://github.com/lvmaple/minas_plugin.git'
    $父目录 = Split-Path -Parent $script:Vendor
    if (-not (Test-Path $父目录)) { New-Item -ItemType Directory -Force -Path $父目录 | Out-Null }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        if (Test-Path $script:Vendor) { Remove-Item $script:Vendor -Recurse -Force }
        & git clone --depth 1 $仓库 $script:Vendor 2>&1 | Out-Null
        if (Test-Path (Join-Path $script:Vendor 'deploy-plugins.py')) { return }
    }
    # zip 兜底（无需安装 git）
    $zip = Join-Path $env:TEMP 'minas_plugin.zip'
    try {
        Invoke-WebRequest -Uri 'https://codeload.github.com/lvmaple/minas_plugin/zip/refs/heads/main' -OutFile $zip -UseBasicParsing
        if (Test-Path $script:Vendor) { Remove-Item $script:Vendor -Recurse -Force }
        Expand-Archive $zip -DestinationPath $父目录 -Force
        Rename-Item (Join-Path $父目录 'minas_plugin-main') $script:Vendor
    } catch {
        停在这里 '组件下载失败' @('检查网络能否访问 github.com', '或手动下载后解压到 vendor\upstream 目录')
    } finally {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path (Join-Path $script:Vendor 'deploy-plugins.py'))) {
        停在这里 '组件不完整' @('删除 vendor 目录后重新运行')
    }
}

# ============================================================
# 第 2 步：找到你的 NAS（自动扫描 → 找不到才问）
# ============================================================
function 步骤-寻找设备 {
    说-步骤 '第 2 步 / 共 6 步：寻找你的 NAS'

    $cfg = 读-配置
    if ($cfg -and $cfg.device.host) {
        if (测-端口 $cfg.device.host 443) {
            说-成功 "NAS 在线：$($cfg.device.host)"
            标记-完成 'device'; return
        }
        说-警告 "配置里的 $($cfg.device.host) 连不上了，重新扫描"
    }

    说-信息 '正在自动扫描局域网（约几秒钟）'
    $证书目录 = Join-Path $env:LOCALAPPDATA 'minasCert'
    $结果 = 跑-Python @((Join-Path $script:Core 'find_nas.py'), '--cert-dir', $证书目录) -允许失败 -静默
    $找到 = $null
    try {
        $解析 = ($结果 | Out-String).Trim() | ConvertFrom-Json
        if ($解析.ok) { $找到 = $解析 }
    } catch { }

    if ($找到) {
        说-成功 "找到了：$($找到.host)"
        $ip = $找到.host
    } else {
        说-警告 '自动扫描没找到（可能是电脑和 NAS 不在同一网段）'
        $ip = 询问 '请手动输入 NAS 的 IP（小米存储 App → 设备详情里能看到）'
        if (-not $ip) { 停在这里 '没有输入 IP' @('确认 NAS 已开机', '电脑和 NAS 接在同一个路由器下') }
    }

    # 生成/更新配置（只此一份，所有脚本共享）
    if (-not $cfg) { $cfg = Get-Content (Join-Path $script:ConfigDir 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json }
    $cfg.device.host = $ip
    if ($找到) {
        # 扫描结果里已带出 UID 和 SNI，省得后面再解析一次
        if ($找到.uid) { $cfg.device.uid = "$($找到.uid)" }
        if ($找到.sni) { $cfg.device.sn  = "$($找到.sni)" }
    }
    $cfg.access.cert_dir = $证书目录
    $cfg.access.key_path = Join-Path $env:USERPROFILE '.ssh\id_ed25519_minas'
    $cfg.access.allowed_ips = @(($ip -replace '\.\d+$', '.0/24'))
    写-配置 $cfg
    说-成功 '配置已保存（下次运行不用再找）'
    标记-完成 'device'
}

# ============================================================
# 第 3 步：root（已部署过会自动跳过）
# ============================================================
function 步骤-获取root {
    说-步骤 '第 3 步 / 共 6 步：获取设备控制权（root）'

    if (测-SSH可用) {
        说-成功 '检测到这台 NAS 已经部署过了，跳过 root'
        标记-完成 'root'; 标记-完成 'ssh'; return
    }

    # 先解析设备身份（UID/SN），后面装插件要用
    说-信息 '读取设备身份信息'
    $身份 = 跑-Python @((Join-Path $script:Core 'nowsl_run.py'), 'info') -允许失败 -静默
    $文本 = ($身份 | Out-String)
    $cfg = 读-配置
    if ($文本 -match 'UID=(\S+)') { $cfg.device.uid = $Matches[1] }
    if ($文本 -match 'SN=(\S+)')  { $cfg.device.sn  = $Matches[1] }
    if ($cfg.device.uid) { 写-配置 $cfg; 说-成功 "设备身份确认（UID=$($cfg.device.uid)）" }

    # 写预演：确认注入链在这台设备上仍然有效，不通过就停手
    说-信息 '安全检查：验证注入通道（约 10 秒，不会对设备做任何修改）'
    $预演 = 跑-Python @((Join-Path $script:Core 'write_probe.py')) -允许失败 -静默
    if (($预演 | Out-String) -match 'NO-GO') {
        停在这里 '安全检查未通过：当前固件可能已修复注入漏洞' @(
            '把这一屏的信息截图发给项目 Issues',
            '不要继续操作，设备没有被修改，是安全的')
    }
    说-成功 '安全检查通过'

    # 正式 root
    Write-Host ''
    Write-Host '  接下来将获取 NAS 的 root 权限（约 2 分钟）。' -ForegroundColor Yellow
    Write-Host '  这利用了固件漏洞，仅限你本人拥有的设备。' -ForegroundColor DarkGray
    if (-not $Yes) {
        $答 = 询问 '确认继续？(y/n)' 'y'
        if ($答 -notmatch '^(y|yes|是)$') { 停在这里 '已取消' @('设备未被修改') }
    }
    $cfg = 读-配置
    $参数 = @((Join-Path $script:Core 'nowsl_run.py'), 'root', $cfg.device.host,
              '--cert-dir', $cfg.access.cert_dir, '--shell-port', "$($cfg.device.shell_port)")
    if ($cfg.device.uid) { $参数 += @('--uid', "$($cfg.device.uid)") }
    跑-Python $参数

    if (-not (等-端口 $cfg.device.host ([int]$cfg.device.shell_port) 90 'root 通道')) {
        停在这里 'root 通道没有建立起来' @('重新双击一键部署重试', '若反复失败，把输出截图发到项目 Issues')
    }
    说-成功 'root 权限获取成功'
    标记-完成 'root'
}

# ============================================================
# 第 4 步：固化 SSH（重启不掉）+ 可选密码兜底
# ============================================================
function 步骤-固化SSH {
    说-步骤 '第 4 步 / 共 6 步：让 SSH 重启后也能用'
    if ((Test-完成 'ssh') -and (测-SSH可用)) { 说-成功 '已完成，跳过'; return }

    $cfg = 读-配置
    if (-not $RootPassword -and -not $Yes) {
        Write-Host ''
        Write-Host '  要不要给 root 设一个密码？（密钥之外的备用钥匙）' -ForegroundColor Yellow
        Write-Host '  直接回车 = 不设，只用密钥（最安全，推荐）' -ForegroundColor DarkGray
        $secure = Read-Host '  密码' -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if (-not [string]::IsNullOrWhiteSpace($plain)) { $RootPassword = $plain }
    }

    $脚本 = Join-Path $script:Core 'ssh_persist.sh'
    if (测-端口 $cfg.device.host ([int]$cfg.device.shell_port) 1500) {
        # root 通道还活着，走它（刚 root 完的情形）
        $envArg = @()
        if ($RootPassword) { $envArg = @('--env', "MINAS_ROOT_PASSWORD=$RootPassword") }
        跑-Python (@((Join-Path $script:Core 'nowsl_run.py'), 'exec', '--file', $脚本, '--timeout', '240') + $envArg)
    } else {
        # 已重启过，走已有 SSH（前提是公钥已注入）
        传-文件 $脚本 '/tmp/ssh_persist.sh'
        if ($RootPassword) { $null = 跑-SSH "MINAS_ROOT_PASSWORD='$RootPassword' sh /tmp/ssh_persist.sh" }
        else { $null = 跑-SSH 'sh /tmp/ssh_persist.sh' }
    }
    $RootPassword = $null

    if (-not (等-端口 $cfg.device.host 22 45 'SSH')) { 停在这里 'SSH 服务没有起来' @('重跑一键部署') }
    $r = 跑-SSH 'id -u' -静默
    说-成功 'SSH 已固化（重启后自动恢复）'
    标记-完成 'ssh'
}

# ============================================================
# 第 5 步：安装插件 + 保护组件
# ============================================================
function 步骤-安装插件 {
    说-步骤 '第 5 步 / 共 6 步：安装插件（Docker 控制台 / 系统监控 / PC 备份）'
    if (Test-完成 'plugins') { 说-成功 '已完成，跳过'; return }
    $cfg = 读-配置
    $enabled = @($cfg.plugins.enabled)
    说-信息 "将安装：$($enabled -join '、')"
    跑-Python @((Join-Path $script:Core 'nowsl_run.py'), 'deploy', '--select', ($enabled -join ','))
    说-成功 '插件安装完成'
    标记-完成 'plugins'
}

function 步骤-保护组件 {
    说-步骤 '第 6 步 / 共 6 步：安装保护组件（防止重启后插件丢失）'
    if (Test-完成 'stack') { 说-成功 '已完成，跳过'; return }
    $cfg = 读-配置
    if (-not $cfg.device.uid) {
        停在这里 '缺少设备 UID' @('删除 config\config.json 后重新运行')
    }

    # 组装部署包：脚本 + 配置（deploy.json 由此唯一生成）
    $stage = Join-Path $script:Root 'stage'
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Join-Path $stage 'units') | Out-Null
    foreach ($f in 'catalog_guard.py', 'keep_plugins_v4.py', 'keepctl.sh', 'hook_v4.sh',
                   'deploy_v4.sh', 'validate_v4.sh', 'uninstall.sh') {
        Copy-Item (Join-Path $script:Core $f) $stage
    }
    Copy-Item (Join-Path $script:Core 'units\*') (Join-Path $stage 'units')
    Copy-Item (Join-Path $script:Root 'docs\04-运维手册.md') (Join-Path $stage '使用说明.md')

    $ports = @{}
    foreach ($p in $cfg.plugins.enabled) { $ports[$p] = [int]$cfg.plugins.ports.$p }
    $deployJson = [ordered]@{
        device     = [ordered]@{ uid = "$($cfg.device.uid)"; sn = "$($cfg.device.sn)"; shell_port = [int]$cfg.device.shell_port }
        access     = [ordered]@{ allowed_ips = @($cfg.access.allowed_ips) }
        plugins    = [ordered]@{ enabled = @($cfg.plugins.enabled); ports = $ports }
        selfheal   = [ordered]@{ mode = "$($cfg.selfheal.mode)"; calendar = "$($cfg.selfheal.calendar)"; interval = "$($cfg.selfheal.interval)" }
        guard_port = 9310
    }
    [System.IO.File]::WriteAllText((Join-Path $stage 'deploy.json'),
        ($deployJson | ConvertTo-Json -Depth 8), (New-Object System.Text.UTF8Encoding($false)))

    说-信息 '上传组件到 NAS'
    跑-SSH "rm -rf $script:StageDir && mkdir -p $script:StageDir/units" -静默 | Out-Null
    Get-ChildItem $stage -File | ForEach-Object { 传-文件 $_.FullName "$script:StageDir/$($_.Name)" }
    Get-ChildItem (Join-Path $stage 'units') -File | ForEach-Object { 传-文件 $_.FullName "$script:StageDir/units/$($_.Name)" }

    说-信息 '在 NAS 上完成安装（含一次真实同步测试）'
    $sn = if ($cfg.device.sn) { $cfg.device.sn } else { 'unknown' }
    $null = 跑-SSH "sh $script:StageDir/deploy_v4.sh '$sn' '$script:StageDir'"

    # keepctl 管理命令入口
    跑-SSH "ln -sf /data/minas-ssh/keep-plugins/keepctl.sh /data/minas-ssh/keep-plugins/keepctl" -允许失败 -静默 | Out-Null
    传-文件 (Join-Path $script:Core 'keepctl-profile.sh') '/etc/profile.d/keepctl.sh'
    跑-SSH 'chmod 644 /etc/profile.d/keepctl.sh' -允许失败 -静默 | Out-Null
    说-成功 '保护组件已就位'
    标记-完成 'stack'
}

# ============================================================
# 收尾：验收 + 配置 ssh nas 别名 + 成功横幅
# ============================================================
function 步骤-验收 {
    说-步骤 '最终验收'
    $null = 跑-SSH "sh $script:StageDir/validate_v4.sh --reconcile"
    标记-完成 'verify'
}

function 配置-别名 {
    $cfg = 读-配置
    $confPath = Join-Path $env:USERPROFILE '.ssh\config'
    $keyForConf = $cfg.access.key_path -replace '\\', '/'
    $block = @"
Host nas minas
    HostName $($cfg.device.host)
    User $($cfg.access.ssh_user)
    IdentityFile $keyForConf
    ServerAliveInterval 30
    ServerAliveCountMax 3
"@
    $existing = ''
    if (Test-Path $confPath) { $existing = Get-Content $confPath -Raw -Encoding UTF8 }
    if ($existing -match '(?m)^Host\s+nas\b') { return }
    $dir = Split-Path -Parent $confPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -Path $confPath -Value "`r`n$block" -Encoding UTF8
    说-成功 '已配置别名，以后打开终端敲 ssh nas 就能连'
}

function 成功横幅 {
    Write-Host ''
    Write-Host '  ================================================' -ForegroundColor Green
    Write-Host '   🎉 全部完成！' -ForegroundColor Green
    Write-Host '  ================================================' -ForegroundColor Green
    Write-Host ''
    Write-Host '  现在可以：' -ForegroundColor White
    Write-Host '   1. 打开小米存储 App → 应用市场，插件已就绪' -ForegroundColor Gray
    Write-Host '   2. 想进命令行：打开 PowerShell 敲  ssh nas' -ForegroundColor Gray
    Write-Host '   3. 查看保护组件状态：登录后敲  keepctl status' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  放心折腾：重启、关机再开都不会丢。' -ForegroundColor DarkGray
    Write-Host '  升级固件前请先看 docs\05-风险与边界.md。' -ForegroundColor DarkGray
}

# ============================================================
# 卸载
# ============================================================
function 步骤-卸载 {
    说-步骤 '卸载全部组件'
    说-警告 '将停止并移除 SSH、目录守卫、自愈定时器与插件'
    $答 = 询问 '确认卸载？(y/n)' 'n'
    if ($答 -notmatch '^(y|yes|是)$') { 停在这里 '已取消' @() }
    传-文件 (Join-Path $script:Core 'uninstall.sh') '/tmp/uninstall.sh'
    $null = 跑-SSH 'sh /tmp/uninstall.sh'
    Remove-Item $script:StatePath -Force -ErrorAction SilentlyContinue
    说-成功 '卸载完成'
}

# ============================================================
# 主流程
# ============================================================
Write-Host ''
Write-Host '  ┌──────────────────────────────────────┐' -ForegroundColor Cyan
Write-Host '  │   小米智能存储（RP05）一键工具箱      │' -ForegroundColor Cyan
Write-Host '  └──────────────────────────────────────┘' -ForegroundColor Cyan
Write-Host '  仅限你本人拥有的设备 · 操作有风险 · 详见 README' -ForegroundColor DarkGray
Write-Host ''

switch ($Stage) {
    'auto' {
        步骤-环境自检
        步骤-寻找设备
        步骤-获取root
        步骤-固化SSH
        步骤-安装插件
        步骤-保护组件
        步骤-验收
        配置-别名
        成功横幅
    }
    'check'     { 步骤-环境自检; 步骤-寻找设备 }
    'probe'     { $r = 跑-Python @((Join-Path $script:Core 'write_probe.py')) -允许失败; $r | Out-Null }
    'root'      { 步骤-获取root }
    'ssh'       { 步骤-固化SSH }
    'plugins'   { 步骤-安装插件 }
    'stack'     { 步骤-保护组件 }
    'verify'    { 步骤-验收 }
    'uninstall' { 步骤-卸载 }
}
