"""把音频文件解成 16 kHz 单声道 s16le 裸 PCM。

为什么要自己解 wav：Windows 包里不带 ffmpeg（GPL + 30MB），而导入功能
原先无条件 `subprocess.run(["ffmpeg", ...])`，在 Windows 上必然
FileNotFoundError。wav 用标准库 wave 就能读，转采样率/混单声道优先用
audioop（C 实现，很快），没有 audioop（Python 3.13 起移除）时退化成
纯 Python 实现。其它格式仍然交给 ffmpeg，没有 ffmpeg 就报一句人话。
"""

from __future__ import annotations

import array
import shutil
import subprocess
import sys
import wave
from pathlib import Path

TARGET_RATE = 16000

try:                       # 3.11/3.12 有，3.13 起移除
    import audioop         # type: ignore
except Exception:          # pragma: no cover
    audioop = None         # type: ignore


def _resample_py(pcm: bytes, rate: int) -> bytes:
    """纯 Python 线性重采样（16-bit 单声道）。只在没有 audioop 时用。"""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        src.byteswap()
    n = len(src)
    if not n or rate == TARGET_RATE:
        return pcm
    out_n = int(n * TARGET_RATE / rate)
    out = array.array("h", bytes(out_n * 2))
    step = (n - 1) / max(out_n - 1, 1)
    for i in range(out_n):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < n else a
        out[i] = int(a + (b - a) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _to_mono16(raw: bytes, width: int, channels: int, rate: int) -> bytes:
    """任意整型位宽/声道 → 16-bit 单声道，保持原采样率。"""
    if audioop is not None:
        if width == 1:                       # 8-bit wav 是无符号的
            raw = audioop.bias(raw, 1, -128)
        if width != 2:
            raw = audioop.lin2lin(raw, width, 2)
        if channels == 2:
            raw = audioop.tomono(raw, 2, 0.5, 0.5)
        elif channels > 2:
            # 多声道：逐声道叠加取平均
            frame = len(raw) // channels
            acc = array.array("h", bytes(frame * 2))
            tmp = array.array("h")
            for c in range(channels):
                tmp.frombytes(raw[c * frame * 2:(c + 1) * frame * 2])
                if sys.byteorder == "big":
                    tmp.byteswap()
                for i in range(frame):
                    acc[i] += tmp[i] // channels
            if sys.byteorder == "big":
                acc.byteswap()
            raw = acc.tobytes()
        return raw

    # 退化路径：只处理 8/16/32-bit，多声道取前两声道平均
    if width == 2 and channels == 1:
        return raw
    if width == 2:
        a = array.array("h")
        a.frombytes(raw[: len(raw) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        mono = array.array("h", bytes((len(a) // channels) * 2))
        for i in range(len(mono)):
            s = 0
            for c in range(channels):
                s += a[i * channels + c]
            mono[i] = s // channels
        if sys.byteorder == "big":
            mono.byteswap()
        return mono.tobytes()
    return raw


def wav_to_pcm16k(path: str | Path) -> bytes | None:
    """读 wav → 16k/单声道/16-bit 裸 PCM。不是 wav 或缺依赖时返回 None。"""
    try:
        with wave.open(str(path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception:
        return None
    if not raw:
        return b""
    if width not in (1, 2, 3, 4):
        return None
    try:
        mono = _to_mono16(raw, width, channels, rate)
    except Exception:
        return None
    if rate == TARGET_RATE:
        return mono
    if audioop is not None:
        return audioop.ratecv(mono, 2, 1, rate, TARGET_RATE, None)[0]
    return _resample_py(mono, rate)


def find_ffmpeg() -> str | None:
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    here = Path(__file__).resolve().parent
    for rel in ("bin/ffmpeg.exe", "bin/ffmpeg"):
        p = here / rel
        if p.is_file():
            return str(p)
    return None


def to_pcm16k(path: str | Path, on_note=None) -> bytes:
    """统一入口：优先标准库解 wav，其次 ffmpeg，都不可用则抛出可读错误。"""
    pcm = wav_to_pcm16k(path)
    if pcm is not None:
        return pcm
    exe = find_ffmpeg()
    if exe:
        r = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-ac", "1", "-ar", str(TARGET_RATE), "-f", "s16le", "-"],
            capture_output=True,
        )
        if r.returncode != 0 or not r.stdout:
            msg = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("无法解码该音频" + ("：" + msg[-1] if msg else ""))
        return r.stdout
    if on_note:
        on_note("没有找到 ffmpeg，只能导入 wav")
    raise RuntimeError(
        "这个格式需要 ffmpeg 才能解码。请导入 wav，"
        "或把 ffmpeg 放到 bin/ 目录（也可加入 PATH）后重试。"
    )