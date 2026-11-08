"""平台差异集中地：数据目录、音频设备、系统声采集器、子进程控制。

设计原则：其余模块只调用这里的函数，不再出现 avfoundation / ScreenCaptureKit /
WASAPI 之类的字面量，也不写死 macOS 路径。这样 Windows 后端只是在本文件里加分支。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
HERE = Path(__file__).resolve().parent

APP_NAME = "MeetLite"


# ---------------------------------------------------------------- 目录

def data_dir() -> Path:
    """配置与数据库目录。mac: ~/Library/Application Support/MeetLite；
    win: %APPDATA%\\MeetLite；其他: $XDG_DATA_HOME/MeetLite。"""
    if IS_WIN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def legacy_dirs() -> list[Path]:
    """本机其他同类工具可能已下载模型的目录（只读复用，找不到就用自带目录）。"""
    if IS_WIN:
        out: list[Path] = []
        for env in ("LOCALAPPDATA", "APPDATA"):
            b = os.environ.get(env)
            if b:
                out.append(Path(b) / "com.meetily.ai")
        out.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "meetily")
        return out
    if IS_MAC:
        return [Path.home() / "Library" / "Application Support" / "com.meetily.ai"]
    return []


def model_dirs() -> list[Path]:
    """模型搜索路径：先自己的目录（首启动下载到这儿），再其他工具的。"""
    return [data_dir() / "models"] + [d / "models" for d in legacy_dirs()]


# ---------------------------------------------------------------- 系统声采集

def find_bin(name: str) -> str | None:
    """在程序目录里找可执行文件。

    布局：bin/<name>（macOS 常用），或 bin/whisper/<name>、bin/llama/<name>
    ——Windows 包里 whisper 与 llama 各自带同名 ggml*.dll，必须分目录，
    否则后拷的会覆盖前一个，导致其中一个引擎加载失败。"""
    exe = name if name.lower().endswith((".exe", "")) and not IS_WIN else name
    for cand in (name, name + ".exe"):
        direct = HERE / "bin" / cand
        if direct.is_file():
            return str(direct)
        if (HERE / "bin").is_dir():
            for d in sorted((HERE / "bin").iterdir()):
                if d.is_dir():
                    hit = d / cand
                    if hit.is_file():
                        return str(hit)
    return None


def system_capture_bin() -> Path:
    return HERE / "bin" / ("loopcap.exe" if IS_WIN else "audiocap")


def system_audio_allowed() -> bool:
    """系统声音采集是否可用（mac 需屏幕录制授权；Windows 的 WASAPI loopback 无需授权）。"""
    b = system_capture_bin()
    if not b.is_file():
        return False
    if IS_WIN:
        return True
    try:
        out = subprocess.run([str(b), "--check"], capture_output=True, timeout=8)
        return out.stdout.decode("utf-8", "replace").strip() == "granted"
    except Exception:
        return False


# Windows 上 loopcap 原生支持麦克风与系统声、并在内部混音，不需要 ffmpeg
NATIVE_CAPTURE = IS_WIN


def system_capture_argv(rate: int) -> list[str]:
    return [str(system_capture_bin()), "--rate", str(rate)]


def native_capture_argv(rate: int, source: str = "mix", mic_index: int | None = None) -> list[str]:
    """Windows 单进程采集：mix / mic / system 都由 loopcap 内部完成并输出 16k mono s16le。"""
    mode = source if source in ("mix", "mic", "system") else "mix"
    argv = [str(system_capture_bin()), "--rate", str(rate), "--mode", mode]
    if mic_index is not None:
        dev_id = devs_cache_id(mic_index)
        if dev_id:
            argv += ["--in-device", dev_id]
    return argv


def system_capture_benign() -> tuple[str, ...]:
    """采集器正常运行时写进 stderr 的噪声行，判错时要忽略。"""
    if IS_WIN:
        return ("loopcap", "开始采集", "按 Ctrl-C")
    return ("开始录音", "audiocap:", "Press Ctrl-C")


# ---------------------------------------------------------------- 麦克风

def device_list_argv(ffmpeg: str) -> list[str]:
    if IS_WIN:
        return [ffmpeg, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"]
    return [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]


def parse_device_list(stderr: str) -> list[dict]:
    """把 ffmpeg 的设备列表解析成 [{index, name}]。"""
    devs: list[dict] = []
    if IS_WIN:
        # [dshow @ ...] "麦克风 (Realtek Audio)" (audio)
        cur = -1
        for line in stderr.splitlines():
            if "(audio)" in line:
                cur += 1
                name = line.split('"')
                label = name[1] if len(name) > 2 else line.strip()
                devs.append({"index": cur, "name": label})
        return devs
    import re

    in_audio = False
    for line in stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        tail = line.split("]", 1)[-1].strip()          # 去掉 "[AVFoundation indev @ 0x..]"
        m = re.match(r"\[(\d+)\]\s*(.+)$", tail)
        if m:
            devs.append({"index": int(m.group(1)), "name": m.group(2).strip()})
    return devs


def mic_input_argv(ffmpeg: str, index: int, rate: int) -> list[str]:
    """麦克风采集命令：统一输出 16k / 单声道 / s16le 到 stdout。"""
    if IS_WIN:
        name = devs_cache_name(index)
        return [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "dshow", "-i", f"audio={name}",
            "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
        ]
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{index}",
        "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    ]


_devices_cache: list[dict] | None = None


def devs_cache_name(index: int) -> str:
    """Windows 的 dshow 按设备名采集，需先把枚举结果缓存下来。"""
    global _devices_cache
    if _devices_cache is None:
        _devices_cache = list_audio_devices()
    for d in _devices_cache:
        if d["index"] == index:
            return d["name"]
    return ""


def devs_cache_id(index: int) -> str:
    """Windows 上 loopcap 按端点 ID 选设备。"""
    if not IS_WIN:
        return ""
    if _devices_cache is None:
        list_audio_devices()
    for d in (_devices_cache or []):
        if d.get("index") == index:
            return d.get("id", "")
    return ""


def list_audio_devices(ffmpeg: str = "ffmpeg") -> list[dict]:
    global _devices_cache
    if IS_WIN:
        b = system_capture_bin()
        if b.is_file():
            try:
                out = subprocess.run([str(b), "--list"], capture_output=True, text=True, timeout=10)
                if out.returncode == 0 and out.stdout.strip():
                    rows = json.loads(out.stdout)
                    caps = [r for r in rows if r.get("flow") == "capture"]
                    _devices_cache = [{"index": i, "name": r.get("name", ""), "id": r.get("id", "")}
                                      for i, r in enumerate(caps)]
                    return _devices_cache
            except Exception:
                pass
    try:
        p = subprocess.run(device_list_argv(ffmpeg), capture_output=True, text=True, timeout=20)
        err = p.stderr
    except Exception:
        return _devices_cache or []
    devs = parse_device_list(err)
    if devs:
        _devices_cache = devs
    return devs


# ---------------------------------------------------------------- 子进程

def no_window_kwargs() -> dict:
    """Windows 上避免每次 spawn 弹黑框。"""
    if not IS_WIN:
        return {}
    flags = 0x08000000  # CREATE_NO_WINDOW
    return {"creationflags": flags}


def exe_name(base: str) -> str:
    return base + (".exe" if IS_WIN else "")


def parent_alive(ppid0: int) -> bool:
    """看门狗用：父进程是否还活着。Windows 的 os.getppid() 在父进程退出后不会变，
    必须直接查进程句柄。"""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(ppid0))
            if not h:
                return False
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return True
    return os.getppid() == ppid0