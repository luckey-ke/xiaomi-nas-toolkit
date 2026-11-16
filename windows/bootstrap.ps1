<#
.SYNOPSIS
    拉取上游第三方代码到 vendor/upstream。

.DESCRIPTION
    本仓库只包含编排层，不分发第三方代码与二进制。运行前需要把上游公开仓库
    （插件源码、统一部署工具、root 工具）拉到 vendor/upstream 下。

    优先 git clone（可指定分支/标签），没有 git 时退回下载 zip 包。
    已存在则跳过，加 -Force 可强制重拉。

.PARAMETER Repo
    上游仓库地址，默认取 config/config.json 的 upstream.repo。

.PARAMETER Ref
    分支或标签，默认取 upstream.ref（空 = 默认分支）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\bootstrap.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo,
    [string]$Ref,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Vendor = Join-Path $Root 'vendor\upstream'
$ConfigPath = Join-Path $Root 'config\config.json'

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

if (-not $Repo -or -not $Ref) {
    if (Test-Path $ConfigPath) {
        $cfg = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $Repo -and $cfg.upstream) { $Repo = $cfg.upstream.repo }
        if (-not $Ref -and $cfg.upstream) { $Ref = $cfg.upstream.ref }
    }
}
if (-not $Repo) { $Repo = 'https://github.com/lvmaple/minas_plugin.git' }

$deployTool = Join-Path $Vendor 'deploy-plugins.py'
if ((Test-Path $deployTool) -and -not $Force) {
    Say "[skip] 上游代码已存在：$Vendor" 'DarkGray'
    exit 0
}
# 存在但不完整（例如上次中断）时先清掉，否则 git clone 会因目录非空失败
if ((Test-Path $Vendor) -and -not (Test-Path $deployTool)) {
    Say "[reset] vendor/upstream 不完整，清理后重拉" 'Yellow'
    Remove-Item $Vendor -Recurse -Force
}
if ($Force -and (Test-Path $Vendor)) {
    Say "[reset] 删除旧的 vendor/upstream" 'Yellow'
    Remove-Item $Vendor -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Vendor) | Out-Null

$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Say "[git] $Repo" 'Cyan'
    $cloneArgs = @('clone', '--depth', '1')
    if ($Ref) { $cloneArgs += @('--branch', $Ref) }
    $cloneArgs += @($Repo, $Vendor)
    & git @cloneArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git clone 失败（退出码 $LASTEXITCODE）。可手动把上游解压到 $Vendor"
    }
} else {
    Say "[zip] 未安装 git，改为下载 zip 包" 'Yellow'
    $zipUrl = $Repo -replace '\.git$', ''
    $zipUrl = $zipUrl + '/archive/refs/heads/' + ($(if ($Ref) { $Ref } else { 'main' })) + '.zip'
    $tmpZip = Join-Path $env:TEMP ("minas-upstream-{0}.zip" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    Say "       下载 $zipUrl" 'Cyan'
    Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -UseBasicParsing
    $tmpDir = Join-Path $env:TEMP ("minas-upstream-{0}" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
    $inner = Get-ChildItem $tmpDir -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
    Copy-Item (Join-Path $inner.FullName '*') $Vendor -Recurse -Force
    Remove-Item $tmpZip -Force
    Remove-Item $tmpDir -Recurse -Force
    Say "       下载分支：$(if ($Ref) { $Ref } else { 'main' })（若失败请改用 git clone）" 'DarkGray'
}

foreach ($need in @('deploy-plugins.py')) {
    if (-not (Test-Path (Join-Path $Vendor $need))) {
        throw "上游缺少 $need，请确认仓库地址或手动放置到 $Vendor"
    }
}
$rootTool = Get-ChildItem $Vendor -Directory -Filter 'xiaomi-nas-root-tool-*' -ErrorAction SilentlyContinue
if (-not $rootTool) {
    throw "上游缺少 xiaomi-nas-root-tool-* 目录，无法执行 root 步骤"
}
Say "[ok] 上游就绪：$($rootTool[0].Name)" 'Green'

# Python 依赖：root 工具用 keystone-engine 现场汇编触发载荷
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & python -c "import keystone" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "[pip] 安装 keystone-engine" 'Cyan'
        & python -m pip install --quiet --disable-pip-version-check keystone-engine
        if ($LASTEXITCODE -ne 0) {
            Say "[warn] keystone-engine 安装失败，root 步骤会报错，请手动 pip install keystone-engine" 'Yellow'
        }
    } else {
        Say "[ok] keystone-engine 已安装" 'Green'
    }
}
