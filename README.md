# MeetLite

本机运行的轻量会议记录助手：**边录边转写 + 一键生成结构化纪要 + Markdown 导出**。
模型直接复用本机 Meetily 已下载的权重，不额外占空间；界面比 Meetily 小一个数量级，双击即用。

```
录音（麦克风 + 系统声） → 本地 Whisper 流式转写 → Qwen3.5 生成纪要 → 存档 / 导出
```

## 一键使用

```bash
open /Users/yyl1nky/Documents/Projects/MeetLite/dist/MeetLite.app
```

首次启动会做两件事：

1. 后台预热模型（约 40–90 秒，界面可以先用，顶栏徽标会先显示「引擎预热中…」再变成「已连接」）；
2. 第一次点「开始录音」时，macOS 会弹出「想要录制此电脑的屏幕和音频」，点**允许**；
   如果点过拒绝，按界面黄条上的「去授权」跳到 系统设置 → 隐私与安全性 → 屏幕录制，勾选 MeetLite，然后重开应用。

## 界面

| 区域 | 作用 |
| --- | --- |
| 左栏 | 会议列表（标题 / 时间 / 段数），新建、导入音视频；悬停条目右侧 `✕` 删除 |
| 中栏 | 实时转写，每段带时间戳；顶栏有录音按钮、电平条、来源与设备、灵敏度 |
| 右栏 | AI 纪要（模板可选）+ 我的笔记（自动保存） |
| 顶栏右侧 | 导出 Markdown（纪要 + 笔记 + 完整转写） |
| 左下角「显示」 | 白天 / 黑夜 / 跟随系统；切换即时生效，窗口标题栏一起变，选择写入配置、下次启动沿用 |
| 左下角「语言」 | 界面语言 中文 / English 即时切换；下方「转写语言」决定 Whisper 用哪种语言识别（默认自动识别） |

- 采集来源：`麦克风 + 系统声`（默认）、`仅麦克风`、`仅系统声`。开会时用默认那档，远程会议两端都能录到。
- 灵敏度：默认**标准**；房间吵、麦克风增益低时切**高**；误触发（把噪声当人声）时切**低**。
- 快捷键：`⌘R` 重新载入界面，`⌘Q` 退出（会自动结束录音并落盘）。
- 配色：左下角「显示」里选白天/黑夜/跟随系统。默认跟随系统外观，选择存在 `~/Library/Application Support/MeetLite/config.json` 的 `theme` 字段（`light|dark|auto`），换端口、重装应用都不会丢。

## 模型（复用 Meetily 的，不重复下载）

| 用途 | 模型 | 路径 |
| --- | --- | --- |
| 转写 | `ggml-large-v3-turbo-q5_0.bin` | `~/Library/Application Support/com.meetily.ai/models/whisper/` |
| 纪要 | `Qwen3.5-2B-Q4_K_M.gguf` | `~/Library/Application Support/com.meetily.ai/models/summary/` |

`config.py` 会自动发现这两个文件（whisper 优先选文件名带 `turbo` 的，摘要优先选最小的 gguf），
所以只要 Meetily 在、模型在，MeetLite 就不用配置。想换模型，写 `~/Library/Application Support/MeetLite/config.json`：

```json
{
  "whisper_model": "/path/to/ggml-model.bin",
  "summary_model": "/path/to/model.gguf",
  "language": "zh",
  "vad_rms": 0.012,
  "silence_flush_ms": 1200,
  "max_segment_ms": 20000
}
```

## 模型管理（界面里直接换）

左下「⚙ 设置 → 模型 → 管理模型」是一个独立的模型面板，思路和 PCL 的整合包、编辑器插件市场一样：
清单驱动 + 多镜像 + 校验 + 断点续传，装完即用，不用记命令行。

- **清单**：`models.json` 是唯一事实来源（id / 类型 / 文件名 / 大小 / sha256 / 镜像列表）。加模型只改这个文件，界面自动多一行。
- **下载**：按 `sources` 顺序尝试镜像（本机实测 `huggingface.co` 直连会超时，`hf-mirror.com` 正常，所以镜像排在前）；`Range` 断点续传写 `.part`，断了重按继续；下完先校验 sha256 再改名成正式文件。
- **导入**：本地 `.bin`（whisper.cpp）或 `.gguf`（llama.cpp）直接拷贝；给 `zip` 也行，会自动挑包里最大的模型文件。
- **粘贴网址**：任何直链都能下（自建仓库、网盘直链、对象存储都行），文件名按 URL 尾部猜，猜不到就按默认 id 落地。
- **使用**：点「使用」写进 `config.json` 的 `whisper_model` / `summary_model`；换转写模型会顺手停掉旧的 whisper-server，下次录音自动用新的。换纪要模型提示重启一次应用。
- **落地位置**：`~/Library/Application Support/MeetLite/models/{whisper,summary}/`。这个目录在搜索路径里排第一，比 Meetily 的目录优先，所以下载的模型会自动生效，不必覆盖 Meetily 的文件。

清单里现在有 4 项：`Whisper large-v3-turbo (q5_0)`（默认）、`medium`、`small`、`Qwen3.5-2B (Q4_K_M)`。
后两个 whisper 档位是给老机器/Windows 笔记本准备的降级选项。

## 自动更新

客户端能自己拉更新包，不依赖应用商店，也不用重装。左下「⚙ 设置 → 更新」里点「检查更新」；
启动后也会在后台静默查一次，有新版时设置按钮右上角亮一个红点。

- **检查**：`GET /api/update/check` 读一个 JSON 清单（默认 `https://fastly.jsdelivr.net/gh/L1nKy0713/MeetLite@main/update.json`，
  可在 `config.json` 的 `update_url` 改）。清单会**并行**从 jsDelivr 四个 CDN、GitHub raw 和
  Releases API 同时取，取版本号最高的那份（同版本时以 raw/API 为准），所以 CDN 缓存旧清单也不会漏更新；
  实测国内网络 2–7 秒返回。只比对版本号，不发任何机器信息。
- **下载**：`POST /api/update/download` 按当前平台取清单里的 `macos` / `windows` 条目。GitHub Release
  资产在国内常直连超时，所以会先并行探测四个反代（ghfast.top / gh-proxy.com / ghproxy.net /
  hk.gh-proxy.com）与直连，按实测速度择优下载（256 KB 试抓估算整包耗时），全程 `Range` 断点续传，
  下完比对 `sha256`，对不上就删包、换线路重试。实测 2.2 MB 包 5.8–19 秒下完。
- **安装**：`POST /api/update/apply` 把新包换进原位。运行中的 `.app` 不能就地覆盖，
  所以落地一个 `apply.sh`：等服务进程与窗口壳都退出 → `mv` 旧包成 `.old` → `mv` 新包到位 →
  `xattr -dr com.apple.quarantine` → `open` 新版本 → 删 `.old`。任一步失败自动回滚。
  下次启动 `updater.cleanup()` 会清掉残留的 `.old`、脚本和解包目录。
- **不做增量 diff**：整包才 2 MB 左右，模型不进包，补丁方案不值当。
- **不做静默安装**：换包必须重启，正在录音被打断是硬伤，所以最后一步永远由用户点。

### 发版流程（三分钟）

```bash
bash build.sh                                   # 出 dist/MeetLite.app
cd dist && ditto -c -k --sequesterRsrc --keepParent MeetLite.app MeetLite-1.0.1.zip
shasum -a 256 MeetLite-1.0.1.zip                # 得到 sha256
stat -f%z MeetLite-1.0.1.zip                    # 得到字节数
```

把 zip 传到 GitHub Releases，然后更新仓库根的 `update.json`：

```json
{
  "version": "1.0.1",
  "published": "2026-11-08",
  "notes": "修了 VAD 在系统声静音时的误切段；模型面板支持 zip 导入。",
  "macos":   { "url": "https://github.com/…/releases/download/v1.0.1/MeetLite-1.0.1.zip",
               "size": 1082753, "sha256": "…" },
  "windows": { "url": "https://github.com/…/releases/download/v1.0.1/MeetLite-1.0.1-win.zip",
               "size": 4200000, "sha256": "…" }
}
```

`version` 一改，所有在跑的老版本下次查清单就会看到更新。没有 `windows` 条目时 Windows 客户端会提示
「清单里没有当前平台的下载地址」，不影响使用。

## 问题反馈

设置 → 问题反馈。三种走法，按可用性自动降级：

| 通道 | 触发条件 | 用户要做什么 |
| --- | --- | --- |
| webhook | `config.json` 里 `feedback_webhook` 有值 | 点「发送」即可 |
| 本地归档 | 永远执行 | 无（`数据目录/feedback/*.json`） |
| GitHub 预填 | 没有 webhook，或上报失败 | 点状态栏里的「在 GitHub 打开」，用自己账号提交 |

诊断信息只含版本、系统、Python 版本、界面/转写语言、两个模型的文件名与大小、最近 30 行日志；
**不含**会议标题、转写文字、笔记、录音；路径里的用户名会被替换成 `***`。
弹层里能直接看到将要发送的原文，也可以关掉「附带环境信息」或改用「复制内容」自己粘贴。

### 收反馈（两种，任选）

**A. 零后端（立刻能用）**：什么都不配，用户点「发送」后走 GitHub 预填；
反馈以 issue 形式进仓库，label 是 `feedback`。

**B. 加一个接收端（用户不用 GitHub 账号）**：`server/feedback-worker.js` 是 Cloudflare Worker，
用服务端 token 建 issue。部署约 5 分钟：

```bash
npm i -g wrangler && wrangler login
wrangler secret put GH_TOKEN         # GitHub PAT，只需 issues 写权限
wrangler secret put FEEDBACK_TOKEN   # 自定义口令
cd server && wrangler deploy
```

然后把 `https://<worker>.workers.dev/f?k=<FEEDBACK_TOKEN>` 填进每个客户端的
`config.json → feedback_webhook`（或写进打包时的默认 `DEFAULTS`）。

### 每周一 08:00 自动汇总

`.github/workflows/feedback-digest.yml` 用 GitHub Actions 定时跑（UTC 周一 00:00 = 北京周一 08:00）：

1. `gh api` 拉最近 7 天有更新的 `label=feedback` issue；
2. `server/feedback-digest.mjs` 折成 Markdown（类型 / 标题 / 状态 / 链接 + 详情）；
3. 写进 `feedback/digest/<日期>.md` 并提交，同时贴到该次 Actions 的 Summary 页。

想改节奏就改 cron；想立刻拉一次用 `workflow_dispatch`（可传 `days`）。
不需要 GitHub Actions 的话，本机 cron 一样能跑：

```bash
0 8 * * 1 cd ~/MeetLite && gh api -X GET repos/L1nKy0713/MeetLite/issues \
  -f state=all -f labels=feedback -f per_page=100 > /tmp/i.json \
  && node server/feedback-digest.mjs /tmp/i.json > feedback/digest/$(date +\%F).md
```

## 依赖（已在本机装好）

```bash
brew install whisper-cpp ffmpeg   # whisper-server 转写服务、ffmpeg 采集与切片
```

- 转写：`whisper-server` 常驻，`-nt -bs 5 -sns -nth 0.55 -l zh`，参数取自 Meetily 的 `whisper_engine.rs`。
- 纪要：`llama-server --jinja --reasoning-budget 0` 调 Qwen3.5-2B。
  `--reasoning-budget 0` 是速度关键：不加它，2B 模型会先写几百字英文思考链，一份纪要要 13 分钟。
  加上后实测：两三段短会（单块）约 18 秒出稿；一小时的会（多块 map-reduce）通常 1.5–3 分钟。
- 系统声：`bin/audiocap`（ScreenCaptureKit 小程序，源码 `bin/audiocap.swift`）。

## 数据

- 数据库：`~/Library/Application Support/MeetLite/meetlite.sqlite`（表 meetings / segments / summaries / notes）
- 日志：`~/Library/Logs/MeetLite/server.log`
- 删除某次会议：左栏悬停该条，点右上 `✕`（会一并删掉转写与纪要）。
- 退出应用会连带结束 `whisper-server` / `llama-server`；异常退出时服务会在 2 秒内自检父进程并退出，不留孤儿进程。

## 开发模式（不开窗口，浏览器访问）

```bash
cd /Users/yyl1nky/Documents/Projects/MeetLite
./run.sh          # → http://127.0.0.1:8787
```

`run.sh` 支持 `--port 9000`、`--no-warm`。

## 多语言

- **界面语言**：左下角「语言」切中文 / English，不用重启，会议列表、按钮、提示一起换；选择存 `config.json` 的 `ui_lang`。
- **转写语言**：「转写语言」下拉可选自动识别、中文、English、日本語、한국어、Deutsch、Français、Español、Русский。默认**自动识别**——每段单独判语言，中英混说也能各归各（代价是每段多几十毫秒）。
- **纪要语言**：由**模板语言**决定，和会议说什么语言无关。切到 English 界面后模板列表换成 `Standard Meeting Minutes / Daily Standup / Project Sync / Retrospective / Customer Call`，产出的就是英文纪要；也可以用英文模板总结一场中文会议，直接得到可转发的英文版本。
- 术语提示词跟着转写语言走：中文用「灰度发布、VAD、说话人分离…」这类中文术语表，英文用对应英文词表（`config.json` 的 `asr_prompt` / `asr_prompt_en`）。

## 与 Meetily 的对应关系

MeetLite 是独立实现：代码从零写起（Python 标准库 HTTP + 单页 HTML + Swift 外壳 + C++ 采集器），
没有复制 Meetily 的 Rust / Next.js 源码、资源或图标。所谓「逆向」只是读它已发布的二进制与本地数据
（SQLite 表结构、`preferences.json`、模型目录布局）来对齐行为，属于互操作性观察，不涉及规避任何技术保护措施。
产品思路（按会议场景分模板做纪要、双路采集、常驻引擎）是公开可见的功能，实现方式与代码均为自写。
Meetily 本身以 MIT 发布（Copyright (c) 2024 Zackriya Solutions）；若日后引用了它的任何代码或提示词文本，
会在 `THIRD_PARTY.md` 里标注来源并保留其许可声明。项目名称、图标与 Meetily 无关联，也不代表其背书。


| Meetily 能力 | MeetLite 实现 |
| --- | --- |
| 麦克风 + 系统声双路采集 | ffmpeg + ScreenCaptureKit，50ms 帧混音，VAD 自适应切段 |
| Whisper 流式转写 | `whisper-server` 常驻 + 上一段末尾作为 initial_prompt 维持术语连贯 |
| 会议列表 / 详情 | SQLite + 左栏列表，即时保存 |
| 6 套摘要模板 | 5 套模板 × 中英双语（标准会议 / 每日站会 / 项目同步 / 复盘 / 客户沟通） |
| AI 摘要 + 笔记 + 导出 | 纪要流式进度 + 自动保存笔记 + Markdown 导出 |
| 多模型切换（云 API / Ollama） | 仅本地 llama.cpp，换 `summary_model` 即可 |
| 说话人分离 | 暂未实现（Meetily 用 parakeet，需另配） |
| 音频回放对齐 | 暂未实现（数据库已存时间戳，后续可挂播放器） |

## 排错

| 现象 | 处理 |
| --- | --- |
| 录了几分钟没有转写 | 看电平条是否在动；不动就换来源/设备，或把灵敏度调到「高」；系统声需屏幕录制权限 |
| 系统声提示需要权限 | 点黄条「去授权」，勾选 MeetLite，**退出并重开应用**（TCC 权限只在启动时读取）。自更新换过包之后系统会把它当成新程序，屏幕录制授权需要再勾一次——这是 macOS 的机制，不是 bug |
| 纪要生成很慢 | 首次调用会和预热抢 CPU，头一份偏慢；之后短会约 20 秒、长会 1.5–3 分钟。确认 `/api/system` 的 `warm` 已是 `ready` |
| 提示找不到模型 | Meetily 被删了或模型被移走，按上面表格放回去，或在 config.json 指定路径 |
| 中文被转成英文 | `config.json` 的 `language` 设为 `zh`（默认已是） |

## 性能基准

`tools/bench.py` 把「识别 + 纪要」拆开计时，用来比较不同机器 / 不同模型规格：

```bash
python3 tools/bench.py                       # 用 /tmp/meeting_zh.wav
python3 tools/bench.py --wav 你的录音.wav --repeat 3
python3 tools/bench.py --lang en --tpl standard_meeting
python3 tools/bench.py --skip-summary        # 只测转写
```

本机（M 系列 Mac，large-v3-turbo-q5_0 + Qwen3.5-2B-Q4_K_M）实测 55.7 秒中文录音：
识别 15.9 秒（**RTF 0.287**，即一小时音频约 17 分钟转完）、出 221 字；
纪要 59.4 秒、出 914 字（约 15 字/秒）。脚本末行打印 JSON，便于版本间对比。

## Windows 版进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 平台差异层 `platforms.py`（数据目录 / 设备枚举 / 采集器 / 进程控制） | ✅ 已落地 |
| M1 | `bin/loopcap/loopcap.cpp`：WASAPI loopback + 麦克风，内部混音，输出 16k mono s16le | ✅ `dist/loopcap.exe` 3.6 MB，静态链接无额外 dll 依赖 |
| M2 | 采集层接入：`platforms.NATIVE_CAPTURE` + `Recorder.start()` 单进程路径 | ✅ 已落地 |
| M3 | C# `.NET Framework 4.8` + WebView2 原生外壳（`windows/MeetLite.Win/`） | ✅ `MeetLite.exe` 81 KB，交叉编译通过 |
| M4 | `windows/build-win.ps1` 打包 + 首启动模型向导 | ✅ 已产出 `MeetLite-1.0.3-win.zip`（23 MB / 解压 49 MB） |
| M5 | 基准脚本 `tools/bench.py` | ✅ 已跑出 macOS 数据（RTF 0.287）；Windows 数据待实机补 |

三个当初的坑，最后都是这么解决的：

- **不要 .NET 8**。目标框架改 `net48`——Win10/11 自带 .NET Framework 4.8，双击就跑，
  不需要用户再装几十 MB 的桌面运行时。代价是 `ProcessStartInfo.ArgumentList`、
  `Process.Kill(entireProcessTree)`、`TakeLast`、`ApplicationConfiguration.Initialize()`
  这些 .NET Core 专有 API 要换写法（`dotnet build` 会在编译期全部报出来）。
- **VC++ 运行时**。上游 whisper.cpp / llama.cpp 的官方 exe 是 MSVC 动态链接，缺
  `msvcp140.dll` 等就会 `0xc0000135` 静默退出。`windows/redist/` 里固化了一份从
  `vc_redist.x64.exe` 里取出的 x64 运行时（4 个 dll），打包时放进 `app\bin\`。
- **dll 同名冲突**。whisper.cpp 与 llama.cpp 都带 `ggml.dll` / `ggml-base.dll` / `SDL2.dll`，
  平铺在同一个目录会互相覆盖，结果其中一个引擎加载失败。现在两个引擎各自一个子目录：
  `app\bin\whisper\`、`app\bin\llama\`，`platforms.find_bin()` 会往这两层找。

采集层契约已在 macOS 上用等价 stub 实测：`--list` 解析出 capture 端点 → 拼 `--mode mix --in-device`
→ Recorder 收到 16k mono s16le → 切段 → 直喂 whisper 出中文字幕，位序与采样率都正确。

`loopcap.exe` 命令行：

```
loopcap.exe --check                 # 自检端点，可用时打印 ok
loopcap.exe --list                  # 列出输出/输入设备（JSON）
loopcap.exe --mode mix              # 系统声 + 麦克风，内部混音（默认）
loopcap.exe --mode system --rate 16000
loopcap.exe --in-device "{0.0.1.00000000}.{...}"   # 指定麦克风端点
```

构建（macOS 上交叉编译）：`brew install mingw-w64 && bash bin/loopcap/build.sh`。

### 在 Windows 上使用

**一条命令部署（推荐）**

```powershell
powershell -ExecutionPolicy Bypass -File deploy-win.ps1
```

脚本会依次：检查 64 位 / 磁盘空间 → 从 gh-proxy → ghfast → 直连三个通道挑最快的下 `MeetLite-<版本>-win.zip`
→ 校验 SHA-256 → 解压到 `%LOCALAPPDATA%\MeetLite` → 关掉旧实例并覆盖程序文件 → 校验 5 个关键文件
→ 找模型（找不到就提示首次启动下载）→ 建桌面 / 开始菜单快捷方式 → 启动。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `-Version 1.0.4` | 指定版本，默认 1.0.4 |
| `-Url .\MeetLite-1.0.4-win.zip` | 用本地包或内网地址，跳过下载 |
| `-ModelsFrom D:\models` | 从已有目录导入模型（挑最大 `.bin` / `.gguf`），离线机首选 |
| `-AddDefenderExclusion` | 把安装目录加进 Defender 排除（需管理员，解决 loopcap 误报） |
| `-Shortcut None` | 不建快捷方式；也可 `Desktop` / `StartMenu` |
| `-NoLaunch` | 装完不启动 |
| `-Uninstall` | 卸载（保留会议数据，加 `-PurgeData` 一并删） |

批量部署（多台机器）示例：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/L1nKy0713/MeetLite/main/windows/deploy-win.ps1 -OutFile deploy-win.ps1
.\deploy-win.ps1 -ModelsFrom \\fileserver\meetlite-models -AddDefenderExclusion -Shortcut Both -NoLaunch
```

**手工部署**

Releases 里下 `MeetLite-1.0.4-win.zip`（23 MB，解压 49 MB），解压到任意目录，双击 `MeetLite.exe`：

```
MeetLite.exe              81 KB   C# 外壳（.NET Framework 4.8，系统自带）
MeetLite.exe.config
Microsoft.Web.WebView2.Core.dll
Microsoft.Web.WebView2.WinForms.dll
WebView2Loader.dll
meetlite.ico
app\                        Python 后端
  bin\loopcap.exe          3.6 MB  WASAPI loopback + 麦克风采集
  bin\whisper\             whisper.cpp v1.8.2（whisper-server.exe + ggml*.dll）
  bin\llama\               llama.cpp b6000（llama-server.exe + ggml*.dll）
  bin\msvcp140*.dll         VC++ x64 运行时（4 个）
  static\ templates\
runtime\                    Python 3.11.9 embeddable
使用说明.txt
```

首次运行：双击 → 向导里下载两个模型（hf-mirror，断点续传 + sha256）→ 开始录音。
系统声走 WASAPI loopback，不需要虚拟声卡、不需要管理员权限；麦克风与系统声在 `loopcap.exe`
内部混成一路 16k mono s16le 再喂 whisper，界面上三个声源档位在 Windows 上同样可用。

**没有 WebView2 也不影响使用**：外壳会捕获 `CoreWebView2Environment.CreateAsync` 的异常，
自动改用系统浏览器打开 `http://127.0.0.1:<端口>/`，窗口收到托盘，功能完全一致；
装一次 [WebView2 Runtime](https://go.microsoft.com/fwlink/p/?LinkId=2124703) 之后就是内置窗口。

已知限制：整套 Windows 支持只做到「能交叉编译、能打包、采集层契约在 macOS 上用等价 stub
实测通过」，还没在真机 Windows 上跑过（开发机没有 Windows 环境）。

### 自己打包（Windows 上）

需要 .NET SDK（`winget install Microsoft.DotNet.SDK`，只用于编译，产物不依赖它），PowerShell 里执行：

```powershell
cd windows
.\build-win.ps1 -Version 1.1.0
```

脚本做六件事，产物是解压即用的目录 `dist\MeetLite-<版本>-win\` 加同名 zip：

1. `dotnet build` 出 `MeetLite.exe`（net48，不发布自包含运行时）。
2. 下载 Python 3.11 embed 包解到 `runtime\`。
3. 拷 `app\`：`server.py`、采集/模型/更新等模块、`static\`、`templates\`。
4. `app\bin\` 放 `loopcap.exe` + VC++ 运行时；引擎分别放 `app\bin\whisper\`、`app\bin\llama\`。
5. 放图标与「使用说明.txt」。
6. `Compress-Archive` 出 zip 并打印 size / sha256，填进 `update.json` 即可发版。

上游二进制地址（脚本里已写死回退链，因为 latest 版本经常不带 Windows 预编译包）：

| 组件 | tag | 资产 |
| --- | --- | --- |
| whisper.cpp | `v1.8.2`（latest v1.9.5 无资产） | `whisper-bin-x64.zip` 3.8 MB |
| llama.cpp | `b6000`（latest v0.9.0 只有 nightly-tag.txt） | `llama-b6000-bin-win-cpu-x64.zip` 14.4 MB |
| Python | 3.11.9 | `python-3.11.9-embed-amd64.zip` 11.2 MB |
| VC++ 运行时 | — | 从 `vc_redist.x64.exe` 抽取，已固化在 `windows/redist/` |

下载顺序是 `gh-proxy.com` → `ghfast.top` → 直连（实测 ghfast.top 对 release 资产回 403）。

离线打包：先手动把上面三个 zip 放进 `%TEMP%\meetlite-cache\`（文件名分别为
`whisper-bin-x64.zip`、`llama-win-x64.zip`、`python-embed.zip`），再 `build-win.ps1 -LocalCache`。
