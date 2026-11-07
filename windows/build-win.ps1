# MeetLite Windows 打包脚本（在 Windows 上运行）
#
#   powershell -ExecutionPolicy Bypass -File windows\build-win.ps1
#
# 产出：dist\MeetLite-<版本>-win.zip（解压即用，绿色版）
# 组成：MeetLite.exe（C# + WebView2 外壳）+ app\（Python 后端与静态界面）
#       + runtime\python.exe（官方 embeddable，仅标准库）
#       + app\bin\{loopcap.exe, whisper-server.exe, llama-server.exe}
#
# 需要：.NET SDK 8、可联网（首次拉运行时；可用 -LocalCache 复用已下载文件）
param(
    [string]$Version = "",
    [switch]$LocalCache,
    [string]$CacheDir = "$PSScriptRoot\..\.cache\win"
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Root

if (-not $Version) {
    $cfgVersion = Select-String -Path (Join-Path $Root 'config.py') -Pattern 'APP_VERSION\s*=\s*"([\d.]+)"'
    if ($cfgVersion) { $Version = $cfgVersion.Matches[0].Groups[1].Value } else { $Version = '0.0.0' }
}
Write-Host "== MeetLite Windows $Version ==" -ForegroundColor Cyan

$Out   = Join-Path $Root 'dist\win'
$Stage = Join-Path $Out 'MeetLite'
New-Item -ItemType Directory -Force -Path $Out, $CacheDir | Out-Null
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage, "$Stage\app", "$Stage\app\bin", "$Stage\runtime" | Out-Null

# ---------------------------------------------------------------- 1. C# 外壳
Write-Host "[1/6] 编译 WebView2 外壳"
dotnet publish (Join-Path $Root 'windows\MeetLite.Win') -c Release -r win-x64 --self-contained false `
    -p:Version=$Version -o $Stage --nologo | Out-Host
if (-not (Test-Path (Join-Path $Stage 'MeetLite.exe'))) { throw "编译失败：没有生成 MeetLite.exe" }

# ---------------------------------------------------------------- 2. Python 运行时
Write-Host "[2/6] 准备 Python 运行时（embeddable 3.11）"
$PyZip = Join-Path $CacheDir 'python-3.11.9-embed-amd64.zip'
if (-not (Test-Path $PyZip)) {
    if ($LocalCache) { throw "缺少 $PyZip，去掉 -LocalCache 让它联网下载" }
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $PyZip
}
Expand-Archive $PyZip -DestinationPath $Stage\runtime -Force
# embeddable 的 _pth 默认不把当前目录加进 sys.path，我们要它——补一行 "."
Get-ChildItem "$Stage\runtime\python*._pth" | ForEach-Object {
    $c = Get-Content $_.FullName
    if ($c -notcontains '.') { Add-Content $_.FullName '.' }
}

# ---------------------------------------------------------------- 3. 后端与界面
Write-Host "[3/6] 拷贝后端"
$files = @('server.py','capture.py','config.py','llm.py','model_manager.py','models.json',
           'platforms.py','store.py','summarize.py','updater.py','whisper_engine.py',
           'update.json','README.md','THIRD_PARTY.md','LICENSE')
foreach ($f in $files) {
    if (Test-Path $f) { Copy-Item $f "$Stage\app\" -Force }
}
Copy-Item 'static'    "$Stage\app\static"    -Recurse -Force
Copy-Item 'templates' "$Stage\app\templates" -Recurse -Force

# ---------------------------------------------------------------- 4. 采集与推理二进制
Write-Host "[4/6] 采集器与推理引擎"
$Loop = Join-Path $Root 'dist\loopcap.exe'
if (-not (Test-Path $Loop)) { $Loop = Join-Path $Root 'bin\loopcap\build\loopcap.exe' }
if (-not (Test-Path $Loop)) { throw "找不到 loopcap.exe，先跑 bash bin/loopcap/build.sh" }
Copy-Item $Loop "$Stage\app\bin\loopcap.exe" -Force

function Get-GithubZip($pattern, $dest) {
    # 依次试 GitHub 直连与常见反代
    $hosts = @('https://github.com/', 'https://ghfast.top/https://github.com/',
               'https://gh-proxy.com/https://github.com/')
    foreach ($h in $hosts) {
        try {
            $url = $h + $pattern
            Write-Host "    ↓ $url"
            Invoke-WebRequest $url -OutFile $dest -TimeoutSec 180
            if ((Get-Item $dest).Length -gt 100000) { return $true }
        } catch { Write-Host "    失败：$($_.Exception.Message)" -ForegroundColor DarkYellow }
    }
    return $false
}

function Get-LatestTag($repo) {
    $j = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -TimeoutSec 60
    return $j.tag_name
}

$WhisperZip = Join-Path $CacheDir 'whisper-bin-x64.zip'
if (-not (Test-Path $WhisperZip)) {
    $tag = Get-LatestTag 'ggml-org/whisper.cpp'
    if (-not (Get-GithubZip "ggml-org/whisper.cpp/releases/download/$tag/whisper-bin-x64.zip" $WhisperZip)) {
        throw "下载 whisper.cpp 二进制失败"
    }
}
$tmp = Join-Path $Out '_whisper'
Expand-Archive $WhisperZip -DestinationPath $tmp -Force
Get-ChildItem $tmp -Recurse -Include 'whisper-server.exe','whisper-cli.exe','*.dll' |
    ForEach-Object { Copy-Item $_.FullName "$Stage\app\bin\" -Force }
Remove-Item $tmp -Recurse -Force

$LlamaZip = Join-Path $CacheDir 'llama-win-x64.zip'
if (-not (Test-Path $LlamaZip)) {
    $tag = Get-LatestTag 'ggml-org/llama.cpp'
    if (-not (Get-GithubZip "ggml-org/llama.cpp/releases/download/$tag/llama-$tag-bin-win-cpu-x64.zip" $LlamaZip)) {
        throw "下载 llama.cpp 二进制失败"
    }
}
$tmp = Join-Path $Out '_llama'
Expand-Archive $LlamaZip -DestinationPath $tmp -Force
Get-ChildItem $tmp -Recurse -Include 'llama-server.exe','*.dll' |
    ForEach-Object { Copy-Item $_.FullName "$Stage\app\bin\" -Force }
Remove-Item $tmp -Recurse -Force

# ---------------------------------------------------------------- 5. 图标与清单
Write-Host "[5/6] 收尾"
Copy-Item 'windows\MeetLite.Win\meetlite.ico' "$Stage\meetlite.ico" -Force
@"
MeetLite $Version（绿色版）
双击 MeetLite.exe 启动。首次启动会提示下载两个模型（转写 574 MB + 纪要 1.2 GB），
也可在设置 → 模型里指定已有文件。

数据与模型目录：%APPDATA%\MeetLite
日志：%APPDATA%\MeetLite\server.log
卸载：直接删除本目录。
"@ | Set-Content "$Stage\使用说明.txt" -Encoding UTF8

# ---------------------------------------------------------------- 6. 打包
Write-Host "[6/6] 打包 zip"
$zip = Join-Path $Root "dist\MeetLite-$Version-win.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$Stage\*" -DestinationPath $zip -CompressionLevel Optimal
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $zip).Length
Write-Host ""
Write-Host "完成：$zip" -ForegroundColor Green
Write-Host "  size   $size"
Write-Host "  sha256 $sha"
Write-Host ""
Write-Host "把下面两行填进 update.json 的 windows 段：" -ForegroundColor Cyan
Write-Host "  `"size`": $size,"
Write-Host "  `"sha256`": `"$sha`""