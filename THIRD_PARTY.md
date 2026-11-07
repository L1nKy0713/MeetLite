# 第三方组件

MeetLite 自身代码（`server.py`、`capture.py`、`static/`、`launcher/`、`bin/audiocap.swift` 等）以 MIT 发布。
下面这些是运行时会用到的外部组件，**不随仓库分发**，由用户机器上的 Homebrew / 官方安装包提供。

| 组件 | 版本（开发机实测） | 许可 | 用途 | 获取方式 |
| --- | --- | --- | --- | --- |
| whisper.cpp（`whisper-server`） | 1.9.5 | MIT | 语音转写 | `brew install whisper-cpp` |
| llama.cpp（`llama-server`） | 0.9.0 | MIT | 纪要生成 | `brew install llama.cpp` |
| ffmpeg | 8.x | LGPL/GPL | 麦克风采集与重采样 | `brew install ffmpeg` |
| Whisper large-v3-turbo（q5_0 ggml） | — | MIT（模型权重） | 默认 ASR 模型 | 复用 Meetily 已下载的文件，或从 HF 下载 |
| Qwen3.5-2B（Q4_K_M gguf） | — | Apache-2.0 | 默认摘要模型 | 同上 |
| ScreenCaptureKit / AppKit / WebKit | macOS 系统框架 | Apple SDK | 系统声采集、原生窗口壳 | 系统自带 |
| Microsoft WebView2（`Microsoft.Web.WebView2`） | 1.0.2903.40 | BSD-3-Clause（NuGet 包）/ 免费专有（运行时） | Windows 界面渲染 | NuGet；运行时由 Edge 提供，缺失时自动降级为系统浏览器 |
| .NET Framework 4.8 | Win10/11 内置 | 微软许可 | Windows 外壳运行时 | 系统自带，无需安装 |
| Microsoft Visual C++ x64 运行时（`msvcp140.dll` 等 4 个） | 14.4x | 微软可再分发许可 | whisper.cpp / llama.cpp 官方 exe 的动态依赖 | 抽取自 `vc_redist.x64.exe`，存放于 `windows/redist/`，随 Windows 包分发 |

## 关于 ffmpeg 与分发

macOS 版用系统自带 ScreenCaptureKit（`bin/audiocap`，MIT）取系统声，ffmpeg 只用于麦克风输入与混音。
Windows 版完全不依赖 ffmpeg 录音：`loopcap.exe`（MIT）直接走 WASAPI loopback + 麦克风并在内部混音，
`ffmpeg` 只在用户导入 mp3/m4a 时才需要（不放也能正常录会议）。
若要把 ffmpeg 打进安装包，需注意它是 GPL/LGPL 构建；Windows 版计划用自写的 `loopcap.exe`（WASAPI loopback + 麦克风）
替代 ffmpeg 的采集职责，从而避免把 GPL 二进制随包分发。

## 与 Meetily 的关系

MeetLite 是独立实现，不包含 Meetily 的代码、资源或图标。两处接触仅为：
1. 读取 Meetily 已下载的模型文件与 SQLite 数据库结构用于对照（只读）。
2. 借鉴「按会议场景分模板做摘要」这一产品思路，模板内容为自写。

Meetily 本身以 MIT 发布（Copyright (c) 2024 Zackriya Solutions），即便如此，本项目不复制其代码。
