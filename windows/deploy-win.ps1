<#
.SYNOPSIS
    MeetLite Windows 一键部署脚本。

.DESCRIPTION
    在目标 Windows 机器上完成：下载发布包 → 校验 SHA-256 → 解压到用户目录 →
    创建桌面/开始菜单快捷方式 →（可选）加 Defender 排除 →（可选）复制模型 → 启动。

    设计目标：
      * 兼容 Windows PowerShell 5.1（Win10 自带）与 PowerShell 7+
      * 默认不需要管理员权限；只有 -AddDefenderExclusion 需要提权
      * 国内网络默认走 gh-proxy 镜像，失败自动回退直连
      * 全程可重复执行（已存在则覆盖安装，配置与会议数据不动）

.PARAMETER Version
    要部署的版本号，默认 1.0.4。会拼成 MeetLite-<Version>-win.zip。

.PARAMETER Url
    直接指定 zip 地址，优先级高于 -Version。用于内网自建分发。

.PARAMETER Target
    安装目录，默认 %LOCALAPPDATA%\MeetLite。

.PARAMETER ModelsFrom
    已有的模型目录（例如从另一台机器复制过来的），内含 whisper\ 与 summary\ 子目录。
    指定后会把这些模型复制到 <Target>\app\models，程序启动即可直接转写。

.PARAMETER AddDefenderExclusion
    把安装目录加入 Microsoft Defender 排除列表（需管理员）。loopcap.exe 未签名，
    部分环境会被启发式误报，批量部署时建议加上。

.PARAMETER Shortcut
    是否创建快捷方式：Both（默认）/Desktop/StartMenu/None。

.PARAMETER NoLaunch
    部署完不自动启动。

.PARAMETER Uninstall
    卸载：删除安装目录、快捷方式、开始菜单项。会议数据（%APPDATA%\MeetLite）默认保留，
    加 -PurgeData 一并删除。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy-win.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy-win.ps1 -Version 1.0.4 -AddDefenderExclusion

.EXAMPLE
    # 内网离线机：先手工放好 zip，再从本机已有模型目录导入
    powershell -ExecutionPolicy Bypass -File deploy-win.ps1 -Url .\MeetLite-1.0.4-win.zip -ModelsFrom D:\MeetLite-models
#>

[CmdletBinding()]
param(
    [string]$Version = "1.0.4",
    [string]$Url = "",
    [string]$Target = "",
    [string]$ModelsFrom = "",
    [string]$Sha256 = "",
    [switch]$AddDefenderExclusion,
    [ValidateSet("Both", "Desktop", "StartMenu", "None")]
    [string]$Shortcut = "Both",
    [switch]$NoLaunch,
    [switch]$Uninstall,
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # 老系统上枚举可能不含 Tls12，忽略
}

# ------------------------------------------------------------------ 常量

$AppName    = "MeetLite"
$RepoOwner  = "L1nKy0713"
$RepoName   = "MeetLite"
# 已知版本的 SHA-256；未知版本的可由 -Sha256 显式传入，或跳过校验
$KnownSha = @{
    "1.0.4" = "b62440091768e23f6e13d586a3a04f0b7e522cbda5397d1dbae4cd6790f1ce93"
    "1.0.3" = "e3cdb994e31ee32bff7933a1d61e12fcb5f3ca28cb90124674fc2887687f14ce"
}
$MirrorPrefixes = @(
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    ""
)

if (-not $Target) {
    $Target = Join-Path $env:LOCALAPPDATA $AppName
}
$DataDir = Join-Path $env:APPDATA $AppName

# ------------------------------------------------------------------ 输出

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Ok($text) {
    Write-Host "    [ok] $text" -ForegroundColor Green
}

function Write-Warn2($text) {
    Write-Host "    [注意] $text" -ForegroundColor Yellow
}

function Write-Err($text) {
    Write-Host "    [失败] $text" -ForegroundColor Red
}

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        # 非 Windows 或身份查询失败时按普通用户处理
        return $false
    }
}

# ------------------------------------------------------------------ 卸载

if ($Uninstall) {
    Write-Step "卸载 $AppName"

    $links = @()
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    $startDir = [Environment]::GetFolderPath("StartMenu")
    if ($desktopDir) { $links += (Join-Path $desktopDir "$AppName.lnk") }
    if ($startDir) { $links += (Join-Path (Join-Path $startDir "Programs") "$AppName.lnk") }
    foreach ($l in $links) {
        if (Test-Path -LiteralPath $l) {
            Remove-Item -LiteralPath $l -Force
            Write-Ok "删除快捷方式 $l"
        }
    }

    Get-Process -Name "$AppName" -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Kill() } catch { }
    }
    Start-Sleep -Milliseconds 600

    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
        Write-Ok "删除安装目录 $Target"
    } else {
        Write-Warn2 "安装目录不存在：$Target"
    }

    if ($PurgeData -and (Test-Path -LiteralPath $DataDir)) {
        Remove-Item -LiteralPath $DataDir -Recurse -Force
        Write-Ok "删除数据目录 $DataDir（会议记录、笔记、配置、模型）"
    } elseif (Test-Path -LiteralPath $DataDir) {
        Write-Warn2 "会议数据保留在 $DataDir；需要一并删除请加 -PurgeData"
    }

    Write-Host ""
    Write-Host "卸载完成。" -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------------ 环境检查

Write-Step "环境检查"

if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Err "只支持 64 位 Windows（10 1809 及以上 / 11）"
    exit 1
}
Write-Ok ("Windows {0} / {1}" -f [Environment]::OSVersion.Version, $env:PROCESSOR_ARCHITECTURE)

$isAdmin = Test-Admin
if ($isAdmin) { Write-Ok "当前为管理员会话" } else { Write-Ok "当前为普通用户会话（无需管理员）" }
if ($AddDefenderExclusion -and -not $isAdmin) {
    Write-Warn2 "-AddDefenderExclusion 需要管理员权限，本次跳过；稍后可用管理员 PowerShell 单独执行"
    $AddDefenderExclusion = $false
}

try {
    $sysDrive = [IO.Path]::GetPathRoot($env:LOCALAPPDATA).TrimEnd('\')
    $freeBytes = (Get-PSDrive -Name $sysDrive).Free
    $freeMb = [math]::Round($freeBytes / 1MB)
    if ($freeMb -lt 2500) {
        Write-Warn2 "系统盘剩余 ${freeMb} MB，安装 + 模型大约需要 2 GB"
    } else {
        Write-Ok "系统盘剩余 ${freeMb} MB"
    }
} catch {
    Write-Warn2 "无法读取系统盘剩余空间，跳过检查"
}

# ------------------------------------------------------------------ 取得安装包

Write-Step "准备安装包"

if (-not $Url) {
    $Url = "https://github.com/$RepoOwner/$RepoName/releases/download/v$Version/$AppName-$Version-win.zip"
}

$isRemote = $Url -match '^https?://'
$zipPath = $null
$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("mlsetup-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

if ($isRemote) {
    $zipPath = Join-Path $tmpDir "$AppName-$Version-win.zip"

    # 先用 1 KB Range 探测各通道，死镜像 12s 内淘汰，比整包超时后再回退快得多
    $chosen = $null
    foreach ($prefix in $MirrorPrefixes) {
        $candidate = if ($prefix) { $prefix + $Url } else { $Url }
        $label = if ($prefix) { ($prefix -replace 'https://', '').TrimEnd('/') } else { "直连 github.com" }
        try {
            Write-Host "    探测 $label ..." -NoNewline
            $resp = Invoke-WebRequest -Uri $candidate -UseBasicParsing -TimeoutSec 12 `
                    -Headers @{ Range = "bytes=0-1023" }
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) {
                Write-Host " 可用" -ForegroundColor Green
                $chosen = @{ Url = $candidate; Label = $label }
                break
            }
            Write-Host " HTTP $($resp.StatusCode)" -ForegroundColor DarkYellow
        } catch {
            Write-Host " 不通" -ForegroundColor DarkYellow
        }
    }

    if (-not $chosen) {
        Write-Err "三个下载通道都不可用：$Url"
        Write-Host "        可手工下载后执行：" -ForegroundColor Yellow
        Write-Host "          powershell -ExecutionPolicy Bypass -File deploy-win.ps1 -Url .\$AppName-$Version-win.zip" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "    从 $($chosen.Label) 下载 ..." -NoNewline
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Invoke-WebRequest -Uri $chosen.Url -OutFile $zipPath -UseBasicParsing -TimeoutSec 900
        $sw.Stop()
        $sizeMb = [math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 1)
        Write-Host " ${sizeMb} MB / $([math]::Round($sw.Elapsed.TotalSeconds,1))s" -ForegroundColor Green
    } catch {
        Write-Host " 失败" -ForegroundColor Red
        Write-Err "下载中断：$($_.Exception.Message)"
        exit 1
    }
} else {
    $resolved = Resolve-Path -LiteralPath $Url -ErrorAction SilentlyContinue
    if (-not $resolved) {
        Write-Err "找不到安装包：$Url"
        exit 1
    }
    $zipPath = $resolved.Path
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        Write-Err "-Url 需要指向一个 .zip 文件，不是目录：$zipPath"
        exit 1
    }
    Write-Ok "使用本地安装包 $zipPath"
}

# ------------------------------------------------------------------ 校验

Write-Step "校验安装包"

$expected = if ($Sha256) { $Sha256.ToLower() } else { $KnownSha[$Version] }
if (-not $expected) {
    Write-Warn2 "版本 $Version 没有内置 SHA-256，跳过完整性校验（可用 -Sha256 显式指定）"
} else {
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Write-Err "SHA-256 不匹配"
        Write-Host "        期望 $expected" -ForegroundColor DarkGray
        Write-Host "        实际 $actual" -ForegroundColor DarkGray
        Write-Err "安装包可能被篡改或下载不完整，已中止"
        exit 1
    }
    Write-Ok "SHA-256 $($actual.Substring(0,16))… 匹配"
}

# ------------------------------------------------------------------ 解压安装

Write-Step "安装到 $Target"

$staging = Join-Path $tmpDir "staging"
New-Item -ItemType Directory -Path $staging -Force | Out-Null
Expand-Archive -LiteralPath $zipPath -DestinationPath $staging -Force
Write-Ok "已解压到临时目录"

# 关掉正在跑的旧实例（覆盖前必须，否则 exe 被占用）
$running = Get-Process -Name "$AppName" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "    关闭正在运行的实例 ..." -NoNewline
    foreach ($p in $running) {
        try { $p.CloseMainWindow() | Out-Null } catch { }
    }
    Start-Sleep -Seconds 2
    Get-Process -Name "$AppName" -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Kill() } catch { }
    }
    Start-Sleep -Milliseconds 800
    Write-Host " 已关闭" -ForegroundColor Green
}

if (-not (Test-Path -LiteralPath $Target)) {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
}

# 覆盖安装：只替换程序文件，不动 models / 用户数据库
Copy-Item -Path (Join-Path $staging "*") -Destination $Target -Recurse -Force
Write-Ok "程序文件已复制"

$required = @(
    "$AppName.exe",
    "Microsoft.Web.WebView2.Core.dll",
    "app\server.py",
    "app\bin\loopcap.exe",
    "runtime\python.exe"
)
$missing = @()
foreach ($rel in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Target $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    Write-Err ("安装不完整，缺少：" + ($missing -join ", "))
    exit 1
}
Write-Ok "关键文件齐全（$($required.Count) 项）"

# ------------------------------------------------------------------ 模型

Write-Step "模型"

$modelRoots = @((Join-Path $Target "app\models"), (Join-Path $DataDir "models"))
foreach ($base in @($env:LOCALAPPDATA, $env:APPDATA, $env:ProgramFiles)) {
    if ($base) { $modelRoots += (Join-Path $base "com.meetily.ai\models") }
}
$modelRoots = @($modelRoots | Where-Object { $_ })

function Find-Model($paths, $pattern) {
    foreach ($p in $paths) {
        if (Test-Path -LiteralPath $p) {
            $hit = Get-ChildItem -LiteralPath $p -Filter $pattern -Recurse -File -ErrorAction SilentlyContinue |
                   Sort-Object Length -Descending | Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

if ($ModelsFrom) {
    if (-not (Test-Path -LiteralPath $ModelsFrom)) {
        Write-Err "模型目录不存在：$ModelsFrom"
        exit 1
    }
    $dst = Join-Path $Target "app\models"
    New-Item -ItemType Directory -Path (Join-Path $dst "whisper") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $dst "summary") -Force | Out-Null
    $whisper = Get-ChildItem -LiteralPath $ModelsFrom -Filter "*.bin" -Recurse -File -ErrorAction SilentlyContinue |
               Sort-Object Length -Descending | Select-Object -First 1
    $gguf = Get-ChildItem -LiteralPath $ModelsFrom -Filter "*.gguf" -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 1
    if ($whisper) {
        Copy-Item -LiteralPath $whisper.FullName -Destination (Join-Path $dst "whisper") -Force
        Write-Ok ("转写模型 {0}（{1} MB）" -f $whisper.Name, [math]::Round($whisper.Length / 1MB))
    } else {
        Write-Warn2 "源目录里没有 .bin 转写模型"
    }
    if ($gguf) {
        Copy-Item -LiteralPath $gguf.FullName -Destination (Join-Path $dst "summary") -Force
        Write-Ok ("摘要模型 {0}（{1} MB）" -f $gguf.Name, [math]::Round($gguf.Length / 1MB))
    } else {
        Write-Warn2 "源目录里没有 .gguf 摘要模型"
    }
} else {
    $w = Find-Model $modelRoots "*.bin"
    $g = Find-Model $modelRoots "*.gguf"
    if ($w -and $g) {
        Write-Ok "已找到现成模型，无需下载"
        Write-Host "        转写 $w" -ForegroundColor DarkGray
        Write-Host "        摘要 $g" -ForegroundColor DarkGray
    } else {
        Write-Warn2 "本机还没有模型，首次启动会弹出下载向导"
        Write-Host "        推荐：ggml-large-v3-turbo-q5_0（574 MB）+ Qwen3.5-2B-Q4_K_M（1.2 GB）" -ForegroundColor DarkGray
        Write-Host "        离线机器可用 -ModelsFrom <目录> 导入，或手工放到：" -ForegroundColor DarkGray
        Write-Host "          $(Join-Path $DataDir 'models\whisper')" -ForegroundColor DarkGray
        Write-Host "          $(Join-Path $DataDir 'models\summary')" -ForegroundColor DarkGray
    }
}

# ------------------------------------------------------------------ Defender

if ($AddDefenderExclusion) {
    Write-Step "Microsoft Defender 排除"
    try {
        Add-MpPreference -ExclusionPath $Target -ErrorAction Stop
        Write-Ok "已排除 $Target"
    } catch {
        Write-Warn2 "添加排除失败：$($_.Exception.Message)"
        Write-Host "        可手工执行：Add-MpPreference -ExclusionPath '$Target'" -ForegroundColor DarkGray
    }
} else {
    Write-Host ""
    Write-Host "    提示：loopcap.exe 未签名，若被 Defender 拦下，用管理员执行" -ForegroundColor DarkGray
    Write-Host "          Add-MpPreference -ExclusionPath '$Target'" -ForegroundColor DarkGray
}

# ------------------------------------------------------------------ 快捷方式

if ($Shortcut -ne "None") {
    Write-Step "快捷方式"
    $exe = Join-Path $Target "$AppName.exe"
    try {
        $shell = New-Object -ComObject WScript.Shell
    } catch {
        Write-Warn2 "无法创建快捷方式（$($_.Exception.Message)），可直接运行 $exe"
        $shell = $null
    }

    $targets = @()
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    $startDir = [Environment]::GetFolderPath("StartMenu")
    if ($desktopDir -and ($Shortcut -eq "Both" -or $Shortcut -eq "Desktop")) {
        $targets += (Join-Path $desktopDir "$AppName.lnk")
    }
    if ($startDir -and ($Shortcut -eq "Both" -or $Shortcut -eq "StartMenu")) {
        $targets += (Join-Path (Join-Path $startDir "Programs") "$AppName.lnk")
    }

    if ($shell) {
        foreach ($lnkPath in $targets) {
            try {
                $lnk = $shell.CreateShortcut($lnkPath)
                $lnk.TargetPath = $exe
                $lnk.WorkingDirectory = $Target
                $lnk.IconLocation = "$exe,0"
                $lnk.Description = "本地会议记录助手：一键录音、自动转写、AI 纪要"
                $lnk.Save()
                Write-Ok "创建 $lnkPath"
            } catch {
                Write-Warn2 "创建 $lnkPath 失败：$($_.Exception.Message)"
            }
        }
    }
}

# ------------------------------------------------------------------ 清理临时文件

Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ 启动

Write-Host ""
if ($NoLaunch) {
    Write-Host "部署完成。启动方式：$Target\$AppName.exe" -ForegroundColor Green
} else {
    Write-Host "启动 $AppName ..." -ForegroundColor Cyan
    Start-Process -FilePath (Join-Path $Target "$AppName.exe") -WorkingDirectory $Target
    Write-Host "首次启动会被 SmartScreen 拦一次，点「更多信息」→「仍要运行」。" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "全部完成。" -ForegroundColor Green
Write-Host "  安装目录 $Target"
Write-Host "  数据目录 $DataDir（会议记录、笔记、配置、模型）"
Write-Host "  卸载     powershell -ExecutionPolicy Bypass -File deploy-win.ps1 -Uninstall"