<#
.SYNOPSIS
    小米智能存储（RP05）一键部署：root → SSH 固化 → 插件安装 → 稳定性栈。

.DESCRIPTION
    分阶段执行，每阶段可单独跑，失败即停且不掩盖错误：

      check     环境自检（Python / 证书 / 上游代码 / 配置 / 设备可达）
      probe     只读预检 + 写预演（不建账号、不写 /etc）
      root      执行 root，拿到临时 root shell（重启后消失）
      ssh       固化 SSH（修补 dropbear、开机自启、开机钩子）
      plugins   安装插件（dockerctl / sysmon / pcbackup）
      stack     部署稳定性栈（目录守卫 + 自愈 + 运行期迁移）
      verify    验收（含一次真实对账）
      all       以上全跑（默认）

.PARAMETER Stage
    要执行的阶段，默认 all。

.PARAMETER ConfigPath
    配置文件路径，默认 config/config.json。

.PARAMETER Yes
    跳过所有交互确认（无人值守用）。

.PARAMETER RootPassword
    设置 root 密码以启用密码登录兜底；不传则交互询问，回答空表示仅用密钥。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\deploy.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\deploy.ps1 -Stage verify
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'check', 'probe', 'root', 'ssh', 'plugins', 'stack', 'verify', 'uninstall')]
    [string]$Stage = 'all',
    [string]$ConfigPath,
    [switch]$Yes,
    [string]$RootPassword
)

$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $PSScriptRoot
$script:Core = Join-Path $script:Root 'core'
$script:Vendor = Join-Path $script:Root 'vendor\upstream'
if (-not $ConfigPath) { $ConfigPath = Join-Path $script:Root 'config\config.json' }
$script:ConfigPath = $ConfigPath
$script:Cfg = $null
$script:Python = 'python'
$script:StageDir = '/data/minas/plugins/catalog/stage'

# ---------- 输出 ----------
function Step($text) { Write-Host ""; Write-Host "==== $text ====" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Info($text) { Write-Host "  [--]   $text" -ForegroundColor Gray }
function Warn($text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "  [FAIL] $text" -ForegroundColor Red }

function Stop-Here($message, $hint) {
    Write-Host ""
    Write-Host "中止：$message" -ForegroundColor Red
    if ($hint) { Write-Host "恢复建议：$hint" -ForegroundColor Yellow }
    exit 1
}

function Confirm-Action($message, $defaultYes = $false) {
    if ($Yes) { return $true }
    $suffix = if ($defaultYes) { '[Y/n]' } else { '[y/N]' }
    $answer = Read-Host "$message $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $defaultYes }
    return $answer -match '^(y|yes|是)$'
}

# ---------- 配置 ----------
function Get-Cfg {
    if ($script:Cfg) { return $script:Cfg }
    if (-not (Test-Path $script:ConfigPath)) { return $null }
    $script:Cfg = Get-Content $script:ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    return $script:Cfg
}

function Save-Cfg {
    $json = $script:Cfg | ConvertTo-Json -Depth 8
    $dir = Split-Path -Parent $script:ConfigPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($script:ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Ok "配置已写入 $script:ConfigPath"
}

function Initialize-Config {
    if (Test-Path $script:ConfigPath) { return }
    Step '首次运行：生成配置'
    $example = Join-Path $script:Root 'config\config.example.json'
    if (-not (Test-Path $example)) { Stop-Here "缺少配置模板 $example" $null }
    $script:Cfg = Get-Content $example -Raw -Encoding UTF8 | ConvertFrom-Json

    $ip = Read-Host '请输入 NAS 的局域网 IP（路由器后台可见）'
    if ([string]::IsNullOrWhiteSpace($ip)) { Stop-Here '未输入 IP' $null }
    $script:Cfg.device.host = $ip.Trim()

    $key = Read-Host 'SSH 私钥路径（回车使用默认 ~/.ssh/id_ed25519_minas）'
    if ([string]::IsNullOrWhiteSpace($key)) {
        $script:Cfg.access.key_path = Join-Path $env:USERPROFILE '.ssh\id_ed25519_minas'
    } else {
        $script:Cfg.access.key_path = $key.Trim()
    }

    $cert = Read-Host '证书目录（回车使用默认 %LOCALAPPDATA%\minasCert）'
    if ([string]::IsNullOrWhiteSpace($cert)) {
        $script:Cfg.access.cert_dir = Join-Path $env:LOCALAPPDATA 'minasCert'
    } else {
        $script:Cfg.access.cert_dir = $cert.Trim()
    }

    $subnet = ($script:Cfg.device.host -replace '\.\d+$', '.0/24')
    $script:Cfg.access.allowed_ips = @($subnet)
    Save-Cfg
    Info "2323 来源白名单初始为同网段 $subnet（可在配置里收紧为单机 IP）"
}

function Assert-Cfg {
    $cfg = Get-Cfg
    if (-not $cfg) { Stop-Here '配置不存在' '先运行 windows\bootstrap.ps1 或首次向导' }
    if ([string]::IsNullOrWhiteSpace($cfg.device.host)) { Stop-Here '配置缺少 device.host' $null }
    if (-not $cfg.access.key_path) {
        $cfg.access.key_path = Join-Path $env:USERPROFILE '.ssh\id_ed25519_minas'
    }
    return $cfg
}

function Resolve-Identity {
    $cfg = Assert-Cfg
    if ($cfg.device.uid -and $cfg.device.sn) { return }
    Info '解析设备身份（SN / UID），只读证书会话'
    $out = Invoke-Py @((Join-Path $script:Core 'nowsl_run.py'), 'info') -AllowFail -Quiet
    $text = ($out | Out-String)
    $uidMatch = [regex]::Match($text, 'UID=(\S+)')
    $snMatch = [regex]::Match($text, 'SN=(\S+)')
    if ($uidMatch.Success -and -not $cfg.device.uid) { $cfg.device.uid = $uidMatch.Groups[1].Value }
    if ($snMatch.Success -and -not $cfg.device.sn) { $cfg.device.sn = $snMatch.Groups[1].Value }
    if ($cfg.device.uid) {
        Save-Cfg
        Ok "UID=$($cfg.device.uid)   SN=$($cfg.device.sn)"
    } else {
        Warn '未能自动解析 UID，请手工填写配置里的 device.uid'
    }
}

# ---------- 基础动作 ----------
function Invoke-Py {
    param([string[]]$PyArgs, [switch]$AllowFail, [switch]$Quiet)
    $output = & $script:Python -X utf8 @PyArgs 2>&1
    $code = $LASTEXITCODE
    if (-not $Quiet) { $output | ForEach-Object { Write-Host $_ } }
    if ($code -ne 0 -and -not $AllowFail) {
        Stop-Here "python 退出码 $code" '看上面的输出定位；可加 -Stage check 复检环境'
    }
    return $output
}

function Invoke-Remote {
    param([string]$RemoteCommand, [switch]$AllowFail, [switch]$Quiet)
    $cfg = Assert-Cfg
    $sshArgs = @(
        '-i', $cfg.access.key_path,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=10',
        '-p', "$($cfg.access.ssh_port)",
        "$($cfg.access.ssh_user)@$($cfg.device.host)",
        $RemoteCommand
    )
    $output = & ssh @sshArgs 2>&1
    $code = $LASTEXITCODE
    if (-not $Quiet) { $output | ForEach-Object { Write-Host $_ } }
    if ($code -ne 0 -and -not $AllowFail) {
        Stop-Here "远程命令失败（退出码 $code）" '确认 SSH 可用：ssh -i <私钥> root@<IP> "id -u"'
    }
    return @{ Code = $code; Output = $output }
}

function Test-Tcp($targetHost, $targetPort, $timeoutMs = 2500) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($targetHost, $targetPort)
        if ($task.Wait($timeoutMs) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-Tcp($targetHost, $targetPort, $seconds, $label) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Tcp $targetHost $targetPort 1500) { return $true }
        Start-Sleep -Seconds 3
    }
    Warn "$label 在 $seconds 秒内未就绪"
    return $false
}

function Send-File($localPath, $remotePath) {
    $cfg = Assert-Cfg
    $dir = ($remotePath -replace '/[^/]+$', '')
    Invoke-Remote "mkdir -p '$dir'" -Quiet | Out-Null
    $scpArgs = @(
        '-i', $cfg.access.key_path,
        '-o', 'StrictHostKeyChecking=accept-new',
        '-P', "$($cfg.access.ssh_port)",
        $localPath,
        "$($cfg.access.ssh_user)@$($cfg.device.host):$remotePath"
    )
    $out = & scp @scpArgs 2>&1
    if ($LASTEXITCODE -ne 0) { Stop-Here "上传 $localPath 失败" ($out -join ' ') }
}

# ---------- 阶段：check ----------
function Invoke-Check {
    Step '阶段 1/7 环境自检'
    $cfg = Assert-Cfg
    Info "设备：$($cfg.device.host)    UID：$(if ($cfg.device.uid) { $cfg.device.uid } else { '(待解析)' })"

    $py = & $script:Python -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>&1
    if ($LASTEXITCODE -ne 0) { Stop-Here 'python 不可用' '安装 Python 3.10+ 并勾选 Add to PATH' }
    $major, $minor = $py.Split('.')
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
        Stop-Here "Python 版本过低（$py）" '本工具需要 Python 3.10 或更新'
    }
    Ok "Python $py"

    & $script:Python -c "import keystone" 2>$null
    if ($LASTEXITCODE -ne 0) { Warn 'keystone-engine 未安装，root 阶段会失败（bootstrap.ps1 会自动装）' }
    else { Ok 'keystone-engine 可用' }

    $deployTool = Join-Path $script:Vendor 'deploy-plugins.py'
    if (-not (Test-Path $deployTool)) { Stop-Here '上游代码缺失' '先运行 windows\bootstrap.ps1' }
    Ok '上游代码就绪'

    $certDir = $cfg.access.cert_dir
    $certs = @()
    if ($certDir -and (Test-Path $certDir)) { $certs = @(Get-ChildItem $certDir -Filter '*_cert.pem') }
    if ($certs.Count -eq 0) {
        Stop-Here "证书目录里没有 *_cert.pem（$certDir）" '先安装「小米智能存储」桌面端并登录目标设备'
    }
    Ok "客户端证书：$($certs[0].Name)"

    if (Test-Tcp $cfg.device.host 443) { Ok '设备 443 可达' }
    else { Stop-Here "连不上 $($cfg.device.host):443" '确认 NAS 已开机、IP 未变（路由器后台查看）' }

    $key = $cfg.access.key_path
    $keyOk = (Test-Path $key) -and (Test-Path "$key.pub")
    if ($keyOk) { Ok "SSH 私钥：$key" } else { Warn "私钥不存在（$key），root 阶段会自动生成" }

    Resolve-Identity
}

# ---------- 阶段：probe ----------
function Invoke-Probe {
    Step '阶段 2/7 只读预检'
    Info '这一步只用证书查询设备状态，不创建账号、不写 /etc'
    Invoke-Py @((Join-Path $script:Core 'nowsl_run.py'), 'probe') -AllowFail | Out-Null

    Step '阶段 2b/7 写预演（MKCOL 控制字符目录名）'
    Info '建一个一次性目录再立刻删掉，确认注入链第一环还在'
    $out = Invoke-Py @((Join-Path $script:Core 'write_probe.py')) -AllowFail
    $text = ($out | Out-String)
    if ($text -match '^GO:' -or $text -match 'GO: MKCOL') {
        Ok '写预演通过：控制字符目录名仍被接受'
    } elseif ($text -match 'NO-GO') {
        Stop-Here '写预演未通过，当前固件可能已修补注入链' '如果设备已被 root 过，可跳过本阶段：-Stage root'
    }
}

# ---------- 阶段：root ----------
function Invoke-Root {
    Step '阶段 3/7 执行 root（获取临时 root shell）'
    $cfg = Assert-Cfg
    Warn '将利用固件漏洞获取 root；仅限你本人拥有的设备'
    if (-not (Confirm-Action '确认继续？')) { Stop-Here '用户取消' $null }

    $pyArgs = @(
        (Join-Path $script:Core 'nowsl_run.py'), 'root',
        $cfg.device.host,
        '--cert-dir', $cfg.access.cert_dir,
        '--shell-port', "$($cfg.device.shell_port)"
    )
    if ($cfg.device.uid) { $pyArgs += @('--uid', "$($cfg.device.uid)") }
    Invoke-Py $pyArgs

    Info '等待 2323 root shell 就绪（最多 90 秒）'
    if (-not (Wait-Tcp $cfg.device.host ([int]$cfg.device.shell_port) 90 'root shell')) {
        Stop-Here 'root shell 未起来' '若日志显示触发成功但缺 socat，可改走 SSH 认证通道直接跑 -Stage ssh'
    }
    Ok "root shell 就绪：$($cfg.device.host):$($cfg.device.shell_port)"
}

# ---------- 阶段：ssh ----------
function Invoke-SshPersist {
    Step '阶段 4/7 固化 SSH'
    $cfg = Assert-Cfg

    if (-not $RootPassword -and -not $Yes) {
        Write-Host ''
        Write-Host '  SSH 默认只认密钥。要不要额外设一个 root 密码作为兜底？' -ForegroundColor Yellow
        Write-Host '  （内网家用风险可控；留空表示仅密钥登录，安全最高）' -ForegroundColor DarkGray
        $secure = Read-Host '  输入 root 密码（留空=不设）' -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if (-not [string]::IsNullOrWhiteSpace($plain)) { $RootPassword = $plain }
    }

    # 2323 通道还活着就用它；否则说明已重启过，退回 SSH
    $useShell = Test-Tcp $cfg.device.host ([int]$cfg.device.shell_port) 1500
    $script = Join-Path $script:Core 'ssh_persist.sh'

    if ($useShell) {
        Info '通过 2323 root shell 执行'
        $envArg = @()
        if ($RootPassword) { $envArg = @('--env', "MINAS_ROOT_PASSWORD=$RootPassword") }
        Invoke-Py (@((Join-Path $script:Core 'nowsl_run.py'), 'exec', '--file', $script, '--timeout', '240') + $envArg)
    } else {
        Warn '2323 不可用，改走已有 SSH（需已注入公钥）'
        Send-File $script '/tmp/ssh_persist.sh'
        if ($RootPassword) {
            Invoke-Remote "MINAS_ROOT_PASSWORD='$RootPassword' sh /tmp/ssh_persist.sh"
        } else {
            Invoke-Remote 'sh /tmp/ssh_persist.sh'
        }
    }
    $RootPassword = $null

    Info '等待 22 端口就绪'
    if (-not (Wait-Tcp $cfg.device.host ([int]$cfg.access.ssh_port) 45 'SSH 22')) {
        Stop-Here 'SSH 未起来' '在重启后仍可用 2323 的情况下重跑 -Stage ssh'
    }
    $id = Invoke-Remote 'id -u; hostname' -Quiet
    Ok "SSH 已就绪（$($id.Output -join ' / ')）"
}

# ---------- 阶段：plugins ----------
function Invoke-Plugins {
    Step '阶段 5/7 安装插件'
    $cfg = Assert-Cfg
    $enabled = @($cfg.plugins.enabled)
    if ($enabled.Count -eq 0) { Stop-Here '配置里 plugins.enabled 为空' $null }
    Info "将安装：$($enabled -join ', ')"

    $select = ($enabled -join ',')
    Invoke-Py @((Join-Path $script:Core 'nowsl_run.py'), 'deploy', '--select', $select)
    Ok '插件安装流程结束'
}

# ---------- 阶段：stack ----------
function Build-Stage {
    $cfg = Assert-Cfg
    $stageLocal = Join-Path $script:Root 'stage'
    if (Test-Path $stageLocal) { Remove-Item $stageLocal -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Join-Path $stageLocal 'units') | Out-Null

    Copy-Item (Join-Path $script:Core 'catalog_guard.py') $stageLocal
    Copy-Item (Join-Path $script:Core 'keep_plugins_v4.py') $stageLocal
    Copy-Item (Join-Path $script:Core 'keepctl.sh') $stageLocal
    Copy-Item (Join-Path $script:Core 'hook_v4.sh') $stageLocal
    Copy-Item (Join-Path $script:Core 'deploy_v4.sh') $stageLocal
    Copy-Item (Join-Path $script:Core 'validate_v4.sh') $stageLocal
    Copy-Item (Join-Path $script:Core 'uninstall.sh') $stageLocal
    Copy-Item (Join-Path $script:Core 'units\*') (Join-Path $stageLocal 'units')
    # 运维手册随部署一起上机，登录后可直接 cat
    Copy-Item (Join-Path $script:Root 'docs\04-运维手册.md') (Join-Path $stageLocal '使用说明.md')

    $ports = @{}
    foreach ($p in $cfg.plugins.enabled) { $ports[$p] = [int]$cfg.plugins.ports.$p }
    $deployJson = [ordered]@{
        device  = [ordered]@{
            uid        = "$($cfg.device.uid)"
            sn         = "$($cfg.device.sn)"
            shell_port = [int]$cfg.device.shell_port
        }
        access  = [ordered]@{
            allowed_ips = @($cfg.access.allowed_ips)
        }
        plugins = [ordered]@{
            enabled = @($cfg.plugins.enabled)
            ports   = $ports
        }
        selfheal = [ordered]@{
            mode     = "$($cfg.selfheal.mode)"
            calendar = "$($cfg.selfheal.calendar)"
            interval = "$($cfg.selfheal.interval)"
        }
        guard_port = 9310
    }
    $json = $deployJson | ConvertTo-Json -Depth 8
    $target = Join-Path $stageLocal 'deploy.json'
    [System.IO.File]::WriteAllText($target, $json, (New-Object System.Text.UTF8Encoding($false)))
    return $stageLocal
}

function Invoke-Stack {
    Step '阶段 6/7 部署稳定性栈'
    $cfg = Assert-Cfg
    if (-not $cfg.device.uid) { Resolve-Identity }
    if (-not $cfg.device.uid) {
        Stop-Here '配置缺少 device.uid（自研插件的用户目录名依赖它）' '手工填入米家数字 UID 后重跑 -Stage stack'
    }

    $stageLocal = Build-Stage
    Info '上传组件到 NAS'
    Invoke-Remote "rm -rf $script:StageDir && mkdir -p $script:StageDir/units" -Quiet | Out-Null
    Get-ChildItem $stageLocal -File | ForEach-Object { Send-File $_.FullName "$script:StageDir/$($_.Name)" }
    Get-ChildItem (Join-Path $stageLocal 'units') -File | ForEach-Object { Send-File $_.FullName "$script:StageDir/units/$($_.Name)" }
    Ok '组件已上传'

    Info '执行部署脚本（含一次真实对账）'
    $sn = if ($cfg.device.sn) { $cfg.device.sn } else { 'unknown' }
    Invoke-Remote "sh $script:StageDir/deploy_v4.sh '$sn' '$script:StageDir'"

    Info '安装 keepctl 命令入口'
    Invoke-Remote "ln -sf /data/minas-ssh/keep-plugins/keepctl.sh /data/minas-ssh/keep-plugins/keepctl" -AllowFail -Quiet | Out-Null
    Send-File (Join-Path $script:Core 'keepctl-profile.sh') '/etc/profile.d/keepctl.sh'
    Invoke-Remote "chmod 644 /etc/profile.d/keepctl.sh" -AllowFail -Quiet | Out-Null
    Ok 'keepctl 已可用（登录后直接敲 keepctl status）'
}

# ---------- 阶段：verify ----------
function Invoke-Verify {
    Step '阶段 7/7 验收（含真实对账）'
    Invoke-Remote "sh $script:StageDir/validate_v4.sh --reconcile"

    Step '写入 SSH 别名（可选，方便日常登录）'
    $cfg = Assert-Cfg
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
    if (Test-Path $confPath) {
        $existing = Get-Content $confPath -Raw -Encoding UTF8
        if ($existing -match '(?m)^Host\s+nas\b') {
            Info '~/.ssh/config 里已有 nas 别名，跳过'
        } else {
            Add-Content -Path $confPath -Value "`r`n$block" -Encoding UTF8
            Ok '已追加 nas 别名，之后可直接 ssh nas'
        }
    } else {
        $dir = Split-Path -Parent $confPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        [System.IO.File]::WriteAllText($confPath, $block, (New-Object System.Text.UTF8Encoding($false)))
        Ok '已创建 ~/.ssh/config，之后可直接 ssh nas'
    }
}

# ---------- 阶段：uninstall ----------
function Invoke-Uninstall {
    Step '卸载'
    Warn '将停止并移除 SSH、目录守卫、自愈定时器与插件单元'
    if (-not (Confirm-Action '确认继续？')) { Stop-Here '用户取消' $null }
    Send-File (Join-Path $script:Core 'uninstall.sh') '/tmp/uninstall.sh'
    Invoke-Remote 'sh /tmp/uninstall.sh'
}

# ---------- 主流程 ----------
Write-Host ''
Write-Host '  小米智能存储（RP05）部署工具' -ForegroundColor White
Write-Host '  仅限你本人拥有的设备；操作有风险，请先读完 README 的风险声明' -ForegroundColor DarkGray

Initialize-Config
if ($Stage -eq 'check') { Invoke-Check; exit 0 }

switch ($Stage) {
    'all' {
        Invoke-Check
        Invoke-Probe
        Invoke-Root
        Invoke-SshPersist
        Invoke-Plugins
        Invoke-Stack
        Invoke-Verify
    }
    'probe' { Invoke-Probe }
    'root' { Invoke-Root }
    'ssh' { Invoke-SshPersist }
    'plugins' { Invoke-Plugins }
    'stack' { Invoke-Stack }
    'verify' { Invoke-Verify }
    'uninstall' { Invoke-Uninstall }
}

Write-Host ''
Write-Host '完成。' -ForegroundColor Green
Write-Host '  日常登录：ssh nas    （或 ssh -i <私钥> root@<IP>）' -ForegroundColor Gray
Write-Host '  查看自愈：keepctl status' -ForegroundColor Gray
Write-Host '  使用说明：cat /data/minas-ssh/keep-plugins/使用说明.md' -ForegroundColor Gray
