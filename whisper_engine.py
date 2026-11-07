"""whisper.cpp server 客户端：常驻进程，模型只加载一次。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import wave
from http.client import HTTPConnection

import platforms

SAMPLE_RATE = 16000


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class WhisperEngine:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.port = int(cfg.get("whisper_port", 8399))
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.ready = False
        self.error: str | None = None

    # ---------- lifecycle ----------
    def _binary(self) -> str | None:
        b = self.cfg.get("whisper_server") or "whisper-server"
        if os.path.isabs(b):
            return b if os.path.isfile(b) else None
        local = Path(__file__).resolve().parent / "bin" / platforms.exe_name(b)
        if local.is_file():
            return str(local)
        return shutil.which(b) or shutil.which(platforms.exe_name(b))

    def _alive(self) -> bool:
        try:
            c = HTTPConnection("127.0.0.1", self.port, timeout=0.8)
            c.request("GET", "/")
            c.getresponse().read()
            return True
        except Exception:
            return False

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        model = self.cfg.get("whisper_model")
        if not model or not os.path.isfile(model):
            raise RuntimeError("未找到 whisper 模型，请在 config.json 里设置 whisper_model")
        bin_path = self._binary()
        if not bin_path:
            raise RuntimeError("未找到 whisper-server，请先执行：brew install whisper-cpp")
        if self._alive():
            self.ready = True
            self.proc = None          # 复用已有服务，不再重复加载模型
            self.log(f"[whisper] 复用 127.0.0.1:{self.port} 上已在运行的服务")
            return
        cmd = [
            bin_path, "-m", model,
            "--host", "127.0.0.1", "--port", str(self.port),
            "-t", str(self.cfg.get("whisper_threads", 6)),
            "-bs", str(self.cfg.get("beam_size", 5)),
            "-nt", "-sns",
            "-nth", str(self.cfg.get("no_speech_thold", 0.55)),
            "-l", self._asr_lang(),
        ]
        self.log(f"[whisper] 启动: {' '.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 120:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server 启动失败（进程退出）")
            try:
                c = HTTPConnection("127.0.0.1", self.port, timeout=1)
                c.request("GET", "/")
                c.getresponse().read()
                self.ready = True
                self.log(f"[whisper] 就绪 {time.time()-t0:.1f}s")
                return
            except Exception:
                time.sleep(0.4)
        raise RuntimeError("whisper-server 启动超时")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.ready = False

    def warmup(self) -> None:
        self.start()

    # ---------- inference ----------
    def _asr_lang(self) -> str:
        v = (self.cfg.get("asr_lang") or "auto").strip().lower()
        if v in ("", "auto", "detect"):
            return "auto"
        return v

    def transcribe(self, pcm: bytes, language: str | None = None, prompt: str = "") -> str:
        if not pcm:
            return ""
        with self.lock:
            self.start()
            wav = pcm_to_wav(pcm)
            boundary = "----MeetLite" + uuid.uuid4().hex
            parts: list[bytes] = []

            def field(name: str, value: str):
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
                )

            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"chunk.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n"
            )
            field("response_format", "json")
            lang = (language or self._asr_lang()).strip().lower()
            field("language", "auto" if lang in ("", "auto", "detect") else lang)
            field("temperature", "0")
            if prompt:
                field("prompt", prompt)
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)

            c = HTTPConnection("127.0.0.1", self.port, timeout=300)
            c.request(
                "POST", "/inference", body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Content-Length": str(len(body))},
            )
            resp = c.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                raise RuntimeError(f"whisper-server {resp.status}: {raw[:200]}")
            try:
                data = json.loads(raw)
                text = data.get("text", "")
            except json.JSONDecodeError:
                text = raw
            return clean_text(text)


def clean_text(t: str) -> str:
    t = (t or "").strip()
    for junk in ("[BLANK_AUDIO]", "[ Silence ]", "(silence)", "[silence]"):
        t = t.replace(junk, "")
    # whisper 有时会在句首重复标点
    t = t.strip(" \n\t")
    while t.startswith(",") or t.startswith("，"):
        t = t[1:].lstrip()
    return t