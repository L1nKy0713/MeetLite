# MeetLite Windows 版下载与安装教程（v1.3.1）

全程本机运行：音频不上传，转写在本地 whisper.cpp，纪要在本地 llama.cpp。只有「下载模型 / 检查更新 / 发反馈」三步会联网。

---

## 0. 先看要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 1809 或更高 / Windows 11，**64 位** |
| 运行库 | WebView2 Runtime（Win11 自带；Win10 多数已随 Edge 装上，缺了也能用，会自动改在浏览器里打开界面） |
| 内存 | 8 GB 起；16 GB 更稳（一个大模型 + 一个 2B 语言模型同时驻留） |
| 磁盘 | 约 2.5 GB（程序 24 MB + 两个模型 1.9 GB + 缓存） |
| 显卡 | 不需要。纯 CPU 也能跑，只是慢一些 |
| 权限 | 不需要管理员权限，绿色版，解压即用 |

---

## 1. 下载

**最新版本：1.3.1**

```
文件名  MeetLite-1.3.1-win.zip
大小    24,243,138 字节
sha256  2392c3345374f26867fdd9147c6cacd6285f2c93707595cb1c1d0742b79e3f42
```

两个地址，内容完全一样，国内优先用第一个（镜像加速）：

- 镜像（推荐）：
  `https://gh-proxy.com/https://github.com/L1nKy0713/MeetLite/releases/download/v1.3.1/MeetLite-1.3.1-win.zip`
- 官方：
  `https://github.com/L1nKy0713/MeetLite/releases/download/v1.3.1/MeetLite-1.3.1-win.zip`
- 发布页（想看更新说明）：
  `https://github.com/L1nKy0713/MeetLite/releases/latest`

### 用 PowerShell 一步到位（推荐）

按 `Win + X` → 选「终端」或「Windows PowerShell」，粘贴：

```powershell
$zip = "$env:USERPROFILE\Downloads\MeetLite-1.3.1-win.zip"
curl.exe -L --retry 3 --retry-delay 2 -o $zip `
  "https://gh-proxy.com/https://github.com/L1nKy0713/MeetLite/releases/download/v1.3.1/MeetLite-1.3.1-win.zip"

# 校验：必须输出 2392C3345374F26867FDD9147C6CACD6285F2C93707595CB1C1D0742B79E3F42
(Get-FileHash $zip -Algorithm SHA256).Hash
"{0:N0} bytes" -f (Get-Item $zip).Length
```

哈希对不上就重下（多半是镜像半途断流），别将就着装。

---

## 2. 解压

绿色版，**不解压到 `C:\Program Files`**（那里需要管理员权限，且托盘程序偶尔会被 UAC 拦）。放用户目录即可：

```powershell
$dir = "$env:LOCALAPPDATA\MeetLite"
Expand-Archive -Path "$env:USERPROFILE\Downloads\MeetLite-1.3.1-win.zip" -DestinationPath $dir -Force
```

也可以用资源管理器：右键 zip → 全部解压缩 → 目标填 `%LOCALAPPDATA%\MeetLite`。

解压后目录应该长这样（共 113 个文件）：

```
%LOCALAPPDATA%\MeetLite\
├─ MeetLite.exe              ← 双击这个
├─ MeetLite.exe.config
├─ WebView2Loader.dll
├─ Microsoft.Web.WebView2.*.dll
├─ meetlite.ico
├─ 使用说明.txt
├─ runtime\python.exe        ← 内嵌 Python，不用另装
└─ app\
   ├─ server.py  models.json  static\  templates\
   └─ bin\
      ├─ loopcap.exe         ← 采音（麦克风 + 系统声）
      ├─ whisper\            ← whisper-server.exe
      └─ llama\              ← llama-server.exe
```

> **不要**把 `bin\whisper` 和 `bin\llama` 里的文件混到一层。两边各带一份同名 `ggml.dll`，混在一起会互相覆盖，引擎起不来。保持解压原样就行。

---

## 3. 首次启动

双击 `MeetLite.exe`。

1. Windows 可能弹 **SmartScreen「Windows 已保护你的电脑」** —— 这是未做代码签名的绿色程序的常规提示。点「更多信息」→「仍要运行」。
2. 出现启动画面（深色，中间写着「正在启动本地引擎…」），它在后台拉起本地服务，一般 3–10 秒。
3. 缺 WebView2 运行时会提示「未找到 WebView2 运行库，正在用系统浏览器打开界面…」，界面改在默认浏览器里开，功能一样。想用内置窗口就装一次 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) 再打开。
4. 首次进来会弹「先准备两个本地模型」。

### 装模型（两种方式，任选）

**方式 A：让它自己下（省事）**

点弹层里的「下载推荐模型」，会依次拉：

| 用途 | 文件 | 大小 |
|---|---|---|
| 转写 | `ggml-large-v3-turbo-q5_0.bin` | 574 MB |
| 写纪要 | `Qwen3.5-2B-Q4_K_M.gguf` | 1.2 GB |

多镜像自动切换（hf-mirror 优先，huggingface 兜底）、断点续传、下完做 sha256 校验。中途关掉也没关系，下次接着下。国内网络大概 5–20 分钟，看带宽。

**方式 B：手动放文件（已经有的直接复用）**

用命令行下（`curl.exe` 是 Win10 1803+ 自带的）：

```powershell
$m = "$env:APPDATA\MeetLite\models"
New-Item -ItemType Directory -Force -Path $m | Out-Null

curl.exe -L -C - --retry 3 -o "$m\ggml-large-v3-turbo-q5_0.bin" `
  "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"

curl.exe -L -C - --retry 3 -o "$m\Qwen3.5-2B-Q4_K_M.gguf" `
  "https://hf-mirror.com/Qwen/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf"
```

校验（两个数都对上才算好）：

```powershell
(Get-FileHash "$m\ggml-large-v3-turbo-q5_0.bin" -Algorithm SHA256).Hash   # 394221709CD5AD1F...FFA7E2
(Get-FileHash "$m\Qwen3.5-2B-Q4_K_M.gguf" -Algorithm SHA256).Hash         # AAF42C8B7C3C...
"{0:N0}" -f (Get-Item "$m\ggml-large-v3-turbo-q5_0.bin").Length           # 574,041,195
"{0:N0}" -f (Get-Item "$m\Qwen3.5-2B-Q4_K_M.gguf").Length                 # 1,280,835,840
```

模型放对位置的判断标准：界面右上角显示 `ASR ggml-large-v3-turbo-q5_0.bin`、`LLM Qwen3.5-2B-Q4_K_M.gguf`，就说明认到了。

> 这台机器上如果已经有用 whisper.cpp / llama.cpp 下过的权重（例如别的同款工具留下的），MeetLite 会自动在常见目录里找到并直接使用，不必重复下载。

---

## 4. 开始一场会议

1. 左上「＋ 新建会议」。
2. 中间选音源：
   - **麦克风 + 系统声** —— 线上会议（腾讯会议 / Zoom / Teams）用它，两边声音都进；
   - **仅麦克风** —— 线下当面开会；
   - **仅系统声** —— 只听电脑里放出来的声音。
3. 点「● 开始录音」。右边「实时转写」逐段出字，每段带时间戳。
4. 讲完点停止，然后切到「AI 纪要」→ 选模板（标准会议纪要 / 每日站会 / 项目同步 / 复盘 / 客户沟通）→「生成」，20 秒到 1 分钟出结构化纪要。
5. 「导出 Markdown」把转写 + 纪要 + 你的笔记一起导出。

「我的笔记」是自动保存的，边开会边记，导出时合在一起。

---

## 5. 日常设置（左下 ⚙）

| 项 | 作用 |
|---|---|
| 显示 | 白天 / 黑夜 / 跟随系统 |
| 语言 · Language | 界面语言中英切换（1.3.1 起双语标注，英文界面显示 Language · 语言） |
| 转写语言 | 自动识别 / 中文 / English / 日本語 / 한국어 / Deutsch / Français / Español / Русский |
| 更新 | 检查更新、下载并安装（原地换壳，配置与会议数据都保留） |
| 问题反馈 | 填标题与描述，带上必要环境信息发给我们（不含会议内容） |
| 回收站 | 删除的会议先丢这里，保留 7 天后自动清理，可随时还原 |
| 关闭窗口时 | 每次询问 / 放到后台 / 直接退出 —— 见下一节 |
| 模型 | 管理模型：下载、导入本地 `.bin`/`.gguf`、换用别的模型 |

---

## 6. 关闭窗口时的行为（1.3.0 起）

点窗口右上角 × 时不一定是退出。三种模式：

- **每次询问**（默认）：弹一个小窗，「放到后台」还是「退出程序」，下面有「总是使用该选项」——勾上就记住，下次不问。
- **放到后台**：窗口消失，录音与转写继续跑，托盘有小图标。双击托盘图标、或再双击 `MeetLite.exe` 就能把窗口叫回来。
- **直接退出**：连本地服务一起停掉。

想改回随时在「设置 → 关闭窗口时」里调，三选一，立即生效。

想彻底关掉时就右键托盘图标 →「退出」，或先改成「直接退出」再点 ×。

---

## 7. 更新

**方式 A（推荐）**：设置 → 更新 →「检查更新」→「下载并安装」。程序会自己挑最快的镜像、断点续传、校验 sha256、原地替换并重启，会议数据不动。

**方式 B**：直接下新的 zip，解压覆盖到同一个目录（`%LOCALAPPDATA%\MeetLite`）。`config.json`、会议库、模型都在 `%APPDATA%\MeetLite`，覆盖程序目录不会丢。

---

## 8. 卸载

```powershell
# 程序
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\MeetLite"

# 配置 + 会议数据 + 模型（想保留会议就别删这个）
Remove-Item -Recurse -Force "$env:APPDATA\MeetLite"
```

没有注册表项、没有服务、没有开机启动项，删目录即干净。

---

## 9. 出问题先看这里

| 现象 | 处理 |
|---|---|
| 双击没反应 / 一闪就没了 | 看日志 `%APPDATA%\MeetLite\server.log`，最后 30 行通常直接写原因 |
| 提示「服务进程意外退出」/「服务启动超时」 | 同上；多半是杀软拦了 `runtime\python.exe` 或 `bin\llama\llama-server.exe`，把它们加进白名单 |
| 界面空白 / 一直转圈 | 端口被占（程序每次随机取空闲端口，一般不会），或 WebView2 异常；先右键托盘退出，再重开 |
| 没有声音 / 转写全是空白 | 音源选错了：线上会议要选「麦克风 + 系统声」；另外系统音量别静音，某些声卡需要在「声音设置 → 录制」里启用「立体声混音」 |
| 转写很慢 | 用的是 `large-v3-turbo`，CPU 上大约 1 小时音频要 20–40 分钟；设置 → 模型里可换 `small` 型号，快 3–5 倍、准确率略降 |
| 纪要生成特别慢（几分钟不出） | 语言模型在「思考」。程序默认已经关掉了思考链（`--reasoning-budget 0`），若换了不支持的版本会自动降级为请求体参数，仍慢就换回推荐模型 |
| 杀软报毒 | 嵌入式 Python + 未签名 exe，误报常见。加白名单；介意就别用 |
| 属性里版本显示 1.1.0.0 | 只是 C# 外壳的版本资源标签没跟着更新，功能与 1.3.1 完全一致，不影响使用 |

需要更多细节：`使用说明.txt` 就在安装目录里；进程、端口、日志三条排查命令：

```powershell
Get-Process MeetLite,python,llama-server,whisper-server -ErrorAction SilentlyContinue |
  Select-Object Name,Id,CPU
Get-Content "$env:APPDATA\MeetLite\server.log" -Tail 30
Select-String -Path "$env:APPDATA\MeetLite\server.log" -Pattern '127\.0\.0\.1:\d+' | Select-Object -Last 1
```

---

## 附：版本对照

| 版本 | 要点 |
|---|---|
| 1.3.1 | 设置页「语言 · Language」中英对照 |
| 1.3.0 | 关闭窗口时可选「放到后台」，带「总是使用该选项」，设置页可改 |
| 1.2.0 | 回收站（7 天）、中英双语、设置面板、模型管理、自更新、问题反馈 |
| 1.1.x | 反馈三通道、delete 键修复 |
| 1.0.x | 首个可用版本：录音 → 实时转写 → 生成纪要 → 导出 |