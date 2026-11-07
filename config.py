"""MeetLite 配置：自动发现已有模型（先自己的目录，再 meetily 的），允许 config.json 覆盖。"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import platforms

APP_VERSION = "1.0.0"
DATA_DIR = platforms.data_dir()
MEETILY_DATA = (platforms.meetily_dirs() or [DATA_DIR])[0]
MODEL_DIRS = platforms.model_dirs()
IS_WIN = platforms.IS_WIN


DEFAULTS = {
    "port": 8787,
    "theme": "auto",          # light | dark | auto
    "perm_dismissed": False,
    "ui_lang": "zh",          # 界面语言 zh | en
    "asr_lang": "auto",       # 转写语言 auto | zh | en | ja | ko | de | fr | es | ru ...
    "language": "zh",         # 旧字段，保留兼容
    "whisper_model": None,
    "summary_model": None,
    "llama_helper": None,
    "llama_server": None,
    "llm_port": 8402,
    "llm_context": 8192,
    "llm_threads": 6,
    "whisper_server": "whisper-server",
    "ffmpeg": "ffmpeg",
    "whisper_threads": 6,
    "beam_size": 5,
    "no_speech_thold": 0.55,
    "vad_rms": 0.012,          # 语音门限（16bit 归一化后 RMS）
    "silence_flush_ms": 1200,  # 静音多久后切段
    "max_segment_ms": 20000,   # 单段最长
    "min_segment_ms": 700,     # 太短的丢弃
    "vad_noise_mult": 2.2,     # 自适应门限 = 底噪 × 该倍数
    "asr_prompt": (
        "以下是普通话会议录音，涉及产品评审、灰度发布、语音转写、VAD、"
        "说话人分离、站会、纪要、预算、导出格式等术语。"
    ),
    "update_url": "https://raw.githubusercontent.com/L1nKy0713/MeetLite/main/update.json",
    "update_check": True,
    "asr_prompt_en": (
        "This is a meeting recording about product reviews, canary releases, "
        "speech transcription, VAD, speaker diarization, standups, meeting minutes, "
        "budgets and export formats."
    ),
}


ASR_LANGS = [
    ("auto", "自动识别 / Auto"),
    ("zh", "中文 Chinese"),
    ("en", "English 英语"),
    ("ja", "日本語 Japanese"),
    ("ko", "한국어 Korean"),
    ("de", "Deutsch German"),
    ("fr", "Français French"),
    ("es", "Español Spanish"),
    ("ru", "Русский Russian"),
]


def asr_hint(cfg) -> str:
    """按转写语言挑提示词；auto 时按界面语言给中文或英文术语提示。"""
    lang = (cfg.get("asr_lang") or "auto").strip().lower()
    if lang.startswith("en"):
        return (cfg.get("asr_prompt_en") or "").strip()
    if lang.startswith("zh"):
        return (cfg.get("asr_prompt") or "").strip()
    ui = (cfg.get("ui_lang") or "zh").strip().lower()
    return (cfg.get("asr_prompt_en") if ui.startswith("en") else cfg.get("asr_prompt") or "").strip()


def _find_whisper_model() -> str | None:
    cands = []
    for root in MODEL_DIRS:
        wdir = root / "whisper"
        if wdir.is_dir():
            files = sorted(wdir.glob("*.bin"), key=lambda p: p.stat().st_size, reverse=True)
            cands += [p for p in files if "turbo" in p.name] + files
    for p in cands:
        return str(p)
    return None


def _find_summary_model() -> str | None:
    for root in MODEL_DIRS:
        sdir = root / "summary"
        if sdir.is_dir():
            files = sorted(sdir.glob("*.gguf"), key=lambda p: p.stat().st_size)
            if files:
                return str(files[0])  # 最小的那个 = Qwen3.5-2B
    return None


def _find_helper() -> str | None:
    # macOS 上才有的 meetily 辅助进程；Windows 走 llama-server
    if platforms.IS_MAC:
        for p in (
            Path("/Users/yyl1nky/WorkBuddy/2026-10-28-21-05-31/meetily/meetily.app/Contents/MacOS/llama-helper"),
            Path("/Applications/meetily.app/Contents/MacOS/llama-helper"),
        ):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return shutil.which("llama-helper")


def _find_llama_server() -> str | None:
    local = Path(__file__).resolve().parent / "bin" / platforms.exe_name("llama-server")
    if local.is_file():
        return str(local)
    found = shutil.which(platforms.exe_name("llama-server")) or shutil.which("llama-server")
    if found:
        return found
    for p in ("/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"):
        if Path(p).is_file():
            return p
    return None


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULTS)
        if path.is_file():
            try:
                self.data.update(json.loads(path.read_text()))
            except Exception:
                pass
        self.data.setdefault("whisper_model", None)
        if not self.data["whisper_model"] or not Path(self.data["whisper_model"]).is_file():
            self.data["whisper_model"] = _find_whisper_model()
        if not self.data["summary_model"] or not Path(self.data["summary_model"]).is_file():
            self.data["summary_model"] = _find_summary_model()
        if not self.data["llama_helper"] or not Path(self.data["llama_helper"]).is_file():
            self.data["llama_helper"] = _find_helper()
        if not self.data.get("llama_server") or not Path(self.data["llama_server"]).is_file():
            self.data["llama_server"] = _find_llama_server()

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

    # 让 dict(cfg) / cfg.copy() / **cfg 正常工作
    def keys(self):
        return self.data.keys()

    def items(self):
        return self.data.items()

    def copy(self) -> dict:
        return dict(self.data)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


def load() -> Config:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Config(DATA_DIR / "config.json")