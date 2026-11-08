# MeetLite Windows 打包脚本（在 Windows 上运行）
#
#   powershell -ExecutionPolicy Bypass -File windows\build-win.ps1
#
# 产出：dist\MeetLite-<版本>-win.zip（解压即用，绿色版）
# 组成：start.bat（唯一入口，CRLF）+ app\（Python 后端与静态界面）
#       + runtime\python.exe（官方 embeddable，仅标准库）
#       + app\bin\{loopcap.exe, whisper-server.exe, llama-server.exe}
#       + MeetLite.exe（可选：装好 .NET SDK 才会编译，编出来 start.bat 会自动优先用它）
#
# 需要：.NET SDK 8、可联网（首次拉运行时；可用 -LocalCache 复用已下载文件）
param(
    [string]$Version = "",
    [switch]$LocalCache,
    [string]$CacheDir = "$PSScriptRoot\..\.cache\win",
    # 没有 .NET SDK（比如在 mac 上拼包）时用 -SkipShell：产物只带 start.bat 浏览器模式
    [switch]$SkipShell,
    # 已经有别人编好的外壳时直接用它，省一次编译
    [string]$ShellExe = ""
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
Write-Host "[1/6] WebView2 外壳"
# 仓库里带了一份真机编过、内嵌 PerMonitorV2 manifest 的外壳；没有 .NET SDK 时直接用它
$prebuilt = Join-Path $Root 'windows\prebuilt\MeetLite.exe'
if (-not $ShellExe -and (Test-Path $prebuilt)) { $ShellExe = $prebuilt }
if ($ShellExe) {
    Copy-Item $ShellExe $Stage -Force
    $cfg = "$ShellExe.config"
    if (Test-Path $cfg) { Copy-Item $cfg $Stage -Force }
    Write-Host "      用现成的外壳：$ShellExe"
} elseif ($SkipShell) {
    Write-Host "      -SkipShell：跳过，产物为 start.bat + 浏览器模式" -ForegroundColor Yellow
} else {
    # 外壳是可选的：没有 .NET SDK 时不该让整条打包链挂掉（mac 上只能走到这里）
    $hasDotnet = $null -ne (Get-Command dotnet -ErrorAction SilentlyContinue)
    if (-not $hasDotnet) {
        Write-Host "      找不到 dotnet，跳过外壳；产物将用 start.bat 起浏览器界面" -ForegroundColor Yellow
    } else {
        try {
            dotnet publish (Join-Path $Root 'windows\MeetLite.Win') -c Release -r win-x64 --self-contained false `
                -p:Version=$Version -o $Stage --nologo | Out-Host
        } catch { Write-Host "      编译失败，继续打浏览器模式：$($_.Exception.Message)" -ForegroundColor Yellow }
    }
}
$hasShell = Test-Path (Join-Path $Stage 'MeetLite.exe')
if ($hasShell) { Write-Host "      外壳：已包含 MeetLite.exe" } else { Write-Host "      外壳：不含（浏览器模式）" }

# ---------------------------------------------------------------- 2. Python 运行时
Write-Host "[2/6] 准备 Python 运行时（embeddable 3.11）"
$PyZip = Join-Path $CacheDir 'python-3.11.9-embed-amd64.zip'
if (-not (Test-Path $PyZip)) {
    if ($LocalCache) { throw "缺少 $PyZip，去掉 -LocalCache 让它联网下载" }
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $PyZip
}
Expand-Archive $PyZip -DestinationPath $Stage\runtime -Force
# embeddable 发行版一旦带 python*._pth 就进入隔离模式：sys.path 完全由这个文件决定，
# 命令行 cwd、脚本所在目录都不会自动加入。而且 _pth 里的相对路径是相对 _pth 自己所在的
# runtime\ 解析的，所以 "." 等于 runtime\，不是 app\。后端模块都在 app\，必须显式写 "..\app"，
# 否则 server.py 一 import audiodec 就是 ModuleNotFoundError、进程秒退（P0）。
Get-ChildItem "$Stage\runtime\python*._pth" | ForEach-Object {
    $c = Get-Content $_.FullName
    if ($c -notcontains '.')     { Add-Content $_.FullName '.' }
    if ($c -notcontains '..\app') { Add-Content $_.FullName '..\app' }
    Write-Host "      _pth: $($_.Name) -> $((Get-Content $_.FullName) -join ' ')"
}

# ---------------------------------------------------------------- 3. 后端与界面
Write-Host "[3/6] 拷贝后端"
# 注意：不要把仓库根的 update.json 拷进包 —— 它的 size/sha256 要等 zip 打完才知道，
# 拷进去必然慢一个版本，而运行时根本不读它（updater.py 只认 config.update_url 的远端清单）。
$files = @('server.py','capture.py','config.py','llm.py','model_manager.py','models.json',
           'platforms.py','store.py','summarize.py','updater.py','whisper_engine.py',
           'feedback.py','audiodec.py','trash.py',
           'README.md','THIRD_PARTY.md','LICENSE')
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

# VC++ 运行时：上游 whisper/llama 官方包是 MSVC 动态链接，缺这几个 dll 会 0xc0000135
$Redist = Join-Path $Root 'windows\redist'
if (Test-Path $Redist) {
    Get-ChildItem $Redist -Filter *.dll | ForEach-Object { Copy-Item $_.FullName "$Stage\app\bin\" -Force }
    Write-Host "      VC++ 运行时 x64：$((Get-ChildItem $Redist -Filter *.dll).Count) 个"
}

function Get-GithubZip($pattern, $dest) {
    # 依次试 GitHub 直连与常见反代
    # 反代优先：直连在中国大陆经常被重置，ghfast.top 对 release 资产会回 403
    $hosts = @('https://gh-proxy.com/https://github.com/',
               'https://ghfast.top/https://github.com/',
               'https://github.com/')
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
    try {
        $j = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -TimeoutSec 60
        return $j.tag_name
    } catch { return $null }
}

# 上游 latest 版本经常不带 Windows 预编译包（whisper.cpp v1.9.x、llama.cpp v0.9.x 都是空资产），
# 所以 latest 失败时回退到已知带 x64 资产的固定 tag。
function Get-TagCandidates($repo, $fallback) {
    $list = @()
    $t = Get-LatestTag $repo
    if ($t) { $list += $t }
    foreach ($f in $fallback) { if ($list -notcontains $f) { $list += $f } }
    return $list
}

$WhisperZip = Join-Path $CacheDir 'whisper-bin-x64.zip'
if (-not (Test-Path $WhisperZip)) {
    $ok = $false
    foreach ($tag in (Get-TagCandidates 'ggml-org/whisper.cpp' @('v1.8.2'))) {
        if (Get-GithubZip "ggml-org/whisper.cpp/releases/download/$tag/whisper-bin-x64.zip" $WhisperZip) { $ok = $true; break }
    }
    if (-not $ok) { throw "下载 whisper.cpp 二进制失败（latest 与回退 tag v1.8.2 都没有 whisper-bin-x64.zip）" }
}
$tmp = Join-Path $Out '_whisper'
Expand-Archive $WhisperZip -DestinationPath $tmp -Force
# whisper 与 llama 各自带同名 ggml*.dll，必须分目录放，否则互相覆盖
New-Item -ItemType Directory -Force -Path "$Stage\app\bin\whisper" | Out-Null
Get-ChildItem $tmp -Recurse -Include 'whisper-server.exe','whisper-cli.exe','*.dll' |
    ForEach-Object { Copy-Item $_.FullName "$Stage\app\bin\whisper\" -Force }
Remove-Item $tmp -Recurse -Force

$LlamaZip = Join-Path $CacheDir 'llama-win-x64.zip'
if (-not (Test-Path $LlamaZip)) {
    $ok = $false
    foreach ($tag in (Get-TagCandidates 'ggml-org/llama.cpp' @('b6000'))) {
        if (Get-GithubZip "ggml-org/llama.cpp/releases/download/$tag/llama-$tag-bin-win-cpu-x64.zip" $LlamaZip) { $ok = $true; break }
    }
    if (-not $ok) { throw "下载 llama.cpp 二进制失败（latest 与回退 tag b6000 都没有 llama-<tag>-bin-win-cpu-x64.zip）" }
}
$tmp = Join-Path $Out '_llama'
Expand-Archive $LlamaZip -DestinationPath $tmp -Force
New-Item -ItemType Directory -Force -Path "$Stage\app\bin\llama" | Out-Null
Get-ChildItem $tmp -Recurse -Include 'llama-server.exe','*.dll' |
    ForEach-Object { Copy-Item $_.FullName "$Stage\app\bin\llama\" -Force }
Remove-Item $tmp -Recurse -Force

# ---------------------------------------------------------------- 5. 图标与清单
Write-Host "[5/6] 收尾"
Copy-Item 'windows\MeetLite.Win\meetlite.ico' "$Stage\meetlite.ico" -Force

# 启动脚本：文件名只用 ASCII（zip 里的中文名在非中文 Windows 上会变乱码），换行必须是 CRLF。
# cmd.exe 靠 CR 判行：LF-only 时 if/括号分组会整段错位，双击后满屏「…不是内部或外部命令」。
$bat = @(
    '@echo off',
    'rem MeetLite launcher. Keep this file CRLF and ASCII-only.',
    'chcp 65001 >nul',
    'setlocal',
    'cd /d "%~dp0"',
    '',
    'if exist "MeetLite.exe" goto useexe',
    'if exist "runtime\python.exe" goto usepython',
    '',
    'echo runtime\python.exe not found. Please extract the zip again.',
    'pause',
    'exit /b 1',
    '',
    ':useexe',
    'start "" "MeetLite.exe"',
    'exit /b 0',
    '',
    ':usepython',
    'echo MeetLite is starting, the UI will open in your browser.',
    'echo This console window is the backend log. Closing it stops MeetLite.',
    '"runtime\python.exe" -X utf8 -u "app\server.py" --port 0 --open',
    'echo.',
    'echo MeetLite stopped.',
    'pause',
    'exit /b 0',
    ''
) -join "`r`n"
[IO.File]::WriteAllText("$Stage\start.bat", $bat, (New-Object System.Text.UTF8Encoding($false)))
$batRead = [IO.File]::ReadAllText("$Stage\start.bat")
if (-not $batRead.Contains("`r`n")) { throw "start.bat 不是 CRLF：cmd.exe 会按行错位，双击必定失败" }
if ($batRead -match "(?<!`r)\n") { throw "start.bat 里混入了裸 LF" }
Write-Host "      start.bat: CRLF ok, $((Get-Item "$Stage\start.bat").Length) 字节"

if ($hasShell) {
    $startMode = "双击 start.bat 启动（会优先用自带的 MeetLite.exe 原生窗口）。"
    $shellNote = @"
关闭窗口时
- 点右上角 × 会问：放到后台 / 退出；勾选「总是使用该选项」就记住。
- 放到后台后程序还活着，托盘图标双击回来。
- 浏览器模式下没有托盘：那个控制台窗口就是程序本体，关掉即退出。
"@
} else {
    $startMode = "双击 start.bat 启动（用内置运行时起服务，并在默认浏览器里打开界面）。"
    $shellNote = @"
关于窗口
- 本包不含原生外壳（MeetLite.exe），界面跑在你的默认浏览器里，功能与原生窗口一致。
- 启动时出现的黑色控制台窗口就是程序本体，关掉它等于退出。
- 想换成原生窗口：在 windows\MeetLite.Win 下 dotnet publish，把生成的 MeetLite.exe 和
  同目录的 dll 拷到本目录，之后双击 start.bat 会自动优先用它。
"@
}
@"
MeetLite $Version（绿色版）
$startMode
首次启动会提示下载两个模型（转写 574 MB + 纪要 1.2 GB），也可在设置 → 模型里指定已有文件。

数据与模型目录：%APPDATA%\MeetLite
日志：%APPDATA%\MeetLite\server.log（控制台窗口里也直接看得到）
卸载：直接删除本目录。

$shellNote
常见问题
- 双击 start.bat 一闪就没了：右键 → 编辑，确认文件没被记事本存成 LF；或直接在本目录
  打开 PowerShell 执行 runtime\python.exe -u app\server.py --port 0 --open 看报错。
- 提示 0xc0000135 / 缺少 msvcp140.dll：本包的 app\bin\ 已自带 VC++ 运行时四个 dll，
  若被清理软件删掉，可从 windows\redist\ 重新拷回。
- 导入了 mp3/m4a 却报解码失败：本包不含 ffmpeg，把 ffmpeg.exe 放进 app\bin\ 即可。
  录会议本身不需要 ffmpeg（系统声由 loopcap.exe 直接采集）。
- 系统声录不到：Windows 设置 → 隐私和安全性 → 麦克风，允许桌面应用访问麦克风；
  播报类应用（Teams/腾讯会议/浏览器）保持正常音量即可，loopcap 走 WASAPI loopback，
  不需要装虚拟声卡，也不需要管理员权限。
"@ | Set-Content "$Stage\README.txt" -Encoding UTF8

# ---------------------------------------------------------------- 5.5 冒烟自检
# 这四个 P0（_pth、缺模型 sys.exit、Environment 大小写、Shown 死锁）都属于
# "构建成功但运行必挂"，编译通过不代表能跑，必须真起一次。
Write-Host "[5.5/6] 冒烟自检"
$PyExe = "$Stage\runtime\python.exe"
if (-not (Test-Path $PyExe)) { throw "缺 runtime\python.exe" }

Write-Host "      1) 后端模块能否 import"
& $PyExe -c "import sys; sys.path.insert(0,'app'); import config, store, platforms, audiodec; print('      ok', config.APP_VERSION)"
if ($LASTEXITCODE -ne 0) { throw "后端 import 失败（多半是 _pth 没补 ..\app）" }

Write-Host "      2) 无模型状态下能否起服务（首启就是没有模型）"
$smokePort = 18787
$env:MEETLITE_SMOKE = '1'
$sm = Start-Process $PyExe -ArgumentList '-X','utf8','-u','server.py','--port',"$smokePort" -WorkingDirectory "$Stage\app" -PassThru -WindowStyle Hidden
$okSmoke = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$smokePort/api/system" -TimeoutSec 3
        if ($r.ok) { $okSmoke = $true; break }
    } catch { }
}
if (-not $sm.HasExited) { Stop-Process $sm -Force }
if (-not $okSmoke) { throw "后端在无模型状态下没能起服务（检查 server.py 里是否还有 sys.exit）" }
Write-Host "      ok 无模型也能起，models_ready=$($r.models_ready)"

if ($hasShell) {
    Write-Host "      3) 外壳能否拉起可见窗口（15s）"
    $shell = Start-Process "$Stage\MeetLite.exe" -PassThru
    $okWin = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        $shell.Refresh()
        if ($shell.HasExited) { break }
        if ($shell.MainWindowHandle -ne [IntPtr]::Zero -and $shell.MainWindowTitle) { $okWin = $true; break }
    }
    if (-not $shell.HasExited) { Stop-Process $shell -Force }
    if (-not $okWin) { throw "外壳没有出现可见窗口（Shown 死锁 / Environment 异常，看 %APPDATA%\MeetLite\server.log）" }
    Write-Host "      ok 窗口已出现"

    # 4) DPI：外壳启动时会往 %APPDATA%\MeetLite\server.log 写一行
    #    [ui] 窗口 client=WxH dpi=D scale=S css=CWxCH
    # CSS 视口 = W/S 才是界面真正的布局宽度，低于 1100 就说明 DPI 感知没生效（1.3.1/1.4.0 的坑）。
    Write-Host "      4) 检查 CSS 视口宽度（DPI 感知是否生效）"
    $log = Join-Path $env:APPDATA 'MeetLite\server.log'
    $cssW = 0
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Path $log) {
            $line = Select-String -Path $log -Pattern '\[ui\] 窗口 client=(\d+)x(\d+) dpi=(\d+)' |
                    Select-Object -Last 1
            if ($line) {
                $cw = [int]$line.Matches[0].Groups[1].Value
                $dpi = [int]$line.Matches[0].Groups[3].Value
                $cssW = [int]($cw * 96 / $dpi)
                break
            }
        }
        Start-Sleep -Milliseconds 500
    }
    if ($cssW -eq 0) {
        Write-Host "      跳过：日志里没有 [ui] 行（外壳可能是旧版）" -ForegroundColor DarkYellow
    } elseif ($cssW -lt 1100) {
        throw "CSS 视口只有 $cssW px（应 ≥1100）：DPI 感知没生效，检查 app.manifest 是否被打进 exe"
    } else {
        Write-Host "      ok CSS 视口 $cssW px"
    }
} else {
    Write-Host "      3) 跳过外壳冒烟（-SkipShell / 无 dotnet）"
}

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