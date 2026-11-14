"""音频采集：麦克风(ffmpeg) + 系统声(平台采集器)，带 VAD 切段。

平台差异全部收敛到 platforms.py：macOS 用 audiocap(ScreenCaptureKit) +
avfoundation；Windows 用 loopcap.exe(WASAPI loopback) + dshow。
"""
from __future__ import annotations

import array
import math
import os
import re
import subprocess
import threading
import time
from collections import deque

RATE = 16000
FRAME_MS = 50
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000  # 1600 bytes

import platforms

HERE = os.path.dirname(os.path.abspath(__file__))


def system_audio_allowed() -> bool:
    return platforms.system_audio_allowed()


def list_input_devices(ffmpeg: str = "ffmpeg") -> list[dict]:
    """返回 [{index, name}] 形式的音频输入设备（平台差异见 platforms.py）。"""
    return platforms.list_audio_devices(ffmpeg)


def rms_of(pcm: bytes) -> float:
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    s = 0
    step = 4 if len(a) > 800 else 1  # 抽样即可，够判断能量
    n = 0
    for i in range(0, len(a), step):
        v = a[i] / 32768.0
        s += v * v
        n += 1
    return math.sqrt(s / max(n, 1))


def segment_pcm(pcm: bytes, max_ms: int = 12000, silence_ms: int = 600,
                gate_min: float = 0.012, preroll_ms: int = 200,
                tail_ms: int = 250) -> list:
    """把整段 16k/mono/s16le PCM 按静音切成 <=max_ms 的片段。

    返回 [(start_ms, end_ms, pcm)]。规则与实时采集一致：门限随环境噪声自适应，
    静音超过 silence_ms 收口，太长强制切。大段静音不产出片段——whisper 在纯静音上
    容易幻听出 "Thank you."。

    为什么不是定长 30 秒：whisper 一次解码只认一个语言标签，30 秒窗口里中英混说时
    非目标语言会被整段丢弃；按句子切之后每段基本是单一语言，`language=auto` 才可靠。
    """
    frame = FRAME_BYTES
    n = len(pcm) // frame
    if n <= 0:
        return []
    out: list[tuple[int, int, bytes]] = []
    noise = 0.004
    voiced_run = 0
    silent_run = 0
    in_speech = False
    start_i = 0
    last_voiced_i = 0
    preroll_frames = max(1, preroll_ms // FRAME_MS)
    tail_frames = max(1, tail_ms // FRAME_MS)
    need_silence = max(1, silence_ms // FRAME_MS)
    max_frames = max(1, max_ms // FRAME_MS)

    def emit(a: int, b: int):
        b = min(b, n)
        a = max(0, min(a, b))
        if b <= a:
            return
        out.append((a * FRAME_MS, b * FRAME_MS, pcm[a * frame:b * frame]))

    for i in range(n):
        level = rms_of(pcm[i * frame:(i + 1) * frame])
        gate = max(gate_min, noise * 2.2)
        voiced = level >= gate
        if not voiced:
            noise = min(gate_min, noise * 0.98 + level * 0.02)
        if voiced:
            voiced_run += 1
            if not in_speech and voiced_run >= 3:
                in_speech = True
                start_i = max(0, i - preroll_frames)
                silent_run = 0
            if in_speech:
                last_voiced_i = i
                silent_run = 0
                if (i - start_i + 1) >= max_frames:
                    emit(start_i, i + 1)
                    in_speech = False
                    voiced_run = 0
        else:
            voiced_run = 0
            if in_speech:
                silent_run += 1
                if silent_run >= need_silence:
                    emit(start_i, last_voiced_i + 1 + tail_frames)
                    in_speech = False
                    silent_run = 0
    if in_speech:
        emit(start_i, last_voiced_i + 1 + tail_frames)

    if not out:  # 整段没测到语音（很安静的录音）：退回定长切片，别把内容丢了
        for j in range(0, n, max_frames):
            emit(j, j + max_frames)
    return out


class _Proc:
    """把某个子进程的 stdout 当 16k/mono/s16le 流，按 50ms 帧推送。"""

    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.on_frame = None
        self.running = False
        self.stopping = False
        self.err = ""

    def start(self, on_frame, on_error=None):
        self.on_frame = on_frame
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            **platforms.no_window_kwargs()
        )
        self.running = True
        self.thread = threading.Thread(target=self._loop, args=(on_error,), daemon=True)
        self.thread.start()

    def _loop(self, on_error):
        assert self.proc and self.proc.stdout
        buf = bytearray()
        total_frames = 0
        while self.running:
            chunk = self.proc.stdout.read(FRAME_BYTES)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= FRAME_BYTES:
                frame = bytes(buf[:FRAME_BYTES])
                del buf[:FRAME_BYTES]
                total_frames += 1
                if self.on_frame:
                    self.on_frame(self.name, frame)
        tail = b""
        if self.proc.stderr:
            try:
                tail = self.proc.stderr.read() or b""
            except Exception:
                pass
        self.err = tail.decode("utf-8", "replace")[-400:]
        was_running = self.running
        self.running = False
        benign = platforms.system_capture_benign()
        clean = all(ln.strip() == "" or any(b in ln for b in benign) for ln in (self.err or "").splitlines()) if self.err else True
        if self.stopping or (clean and self.proc.poll() in (0, -15, None)):
            return
        if "NEED_SCREEN_PERMISSION" in (self.err or ""):
            on_error("NEED_SCREEN_PERMISSION：请在 系统设置 → 隐私与安全性 → 屏幕录制 中勾选 MeetLite，然后重新打开应用")
        elif on_error and was_running and total_frames == 0:
            on_error(f"{self.name} 没有采集到音频: {self.err or '进程提前退出'}")
        elif on_error and self.proc.poll() not in (0, None):
            on_error(f"{self.name} 退出: {self.err}")

    def stop(self):
        self.stopping = True
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _normalize(pcm: bytes, target: float = 0.85, max_gain: float = 6.0) -> bytes:
    """把段落峰值抬到 target，避免小声说话在 whisper 里变成静音。

    只放大不衰减，放大倍数上限 max_gain（约 +15dB）。
    """
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return pcm
    peak = max(max(a), -min(a))
    if peak <= 0:
        return pcm
    gain = min(max_gain, (target * 32767.0) / peak)
    if gain <= 1.05:
        return pcm
    out = array.array("h", (0 for _ in range(len(a))))
    for i, v in enumerate(a):
        x = int(v * gain)
        out[i] = 32767 if x > 32767 else (-32768 if x < -32768 else x)
    return out.tobytes()


class Recorder:
    """采集 -> 静音分段 -> 回调 (start_ms, end_ms, pcm)。"""

    def __init__(self, cfg, on_segment, on_level=None, on_error=None, log=print):
        self.cfg = cfg
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_error = on_error or (lambda m: log(f"[capture] {m}"))
        self.log = log

        self.procs: list[_Proc] = []
        self.thread: threading.Thread | None = None
        self.running = False
        self.t0 = 0.0
        self.frames_seen = 0

        # 分段状态
        self.seg = bytearray()
        self.seg_start_ms = 0
        self.silence_ms = 0
        self.in_speech = False
        self.preroll = deque(maxlen=8)  # 400ms 预卷
        self.noise = 0.004            # 环境噪声估计（EMA）
        self.voiced_run = 0           # 连续有声帧数，避免点击声误触发
        self._silent_frames = 0       # 连续无语音帧计数（用于提示）
        self._no_voice_hint = False

        # 混音
        self.pending: dict[str, bytearray] = {}
        self.cv = threading.Condition()
        self.names: list[str] = []

    # ------------- 帧汇聚 -------------
    def _feed(self, name: str, frame: bytes):
        with self.cv:
            self.pending.setdefault(name, bytearray()).extend(frame)
            self.cv.notify()

    def _next_frame(self) -> tuple[bytes, bool]:
        """等到所有源都有 1 帧后混音返回；超时用已有数据。"""
        wait_s = 0.075  # 麦克风每 50ms 一帧；系统声在静音时不产生数据，不能久等
        with self.cv:
            deadline = time.time() + wait_s
            while self.running:
                if all(len(self.pending.get(n, b"")) >= FRAME_BYTES for n in self.names):
                    break
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self.cv.wait(min(remain, 0.025))
            # 任一源积压超过 2s 就丢最旧的，避免系统声恢复时延迟越来越大
            cap = FRAME_BYTES * 40
            for n in self.names:
                buf = self.pending.get(n)
                if buf is not None and len(buf) > cap:
                    del buf[: len(buf) - cap]
            out = bytearray(FRAME_BYTES)
            got = False
            for n in self.names:
                buf = self.pending.get(n)
                if not buf or len(buf) < FRAME_BYTES:
                    continue
                got = True
                seg = bytes(buf[:FRAME_BYTES])
                del buf[:FRAME_BYTES]
                a = array.array("h"); a.frombytes(seg)
                b = array.array("h"); b.frombytes(bytes(out))
                for i in range(len(a)):
                    v = b[i] + a[i]
                    b[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
                out = bytearray(b.tobytes())
            return bytes(out), got

    # ------------- 主循环 -------------
    def _loop(self):
        self.t0 = time.time()
        while self.running:
            frame, got = self._next_frame()
            if not got:
                continue
            self._process(frame)
        self._flush(final=True)

    def _process(self, frame: bytes):
        level = rms_of(frame)
        if self.on_level:
            self.on_level(level)
        now_ms = int((time.time() - self.t0) * 1000)
        gate_min = float(self.cfg.get("vad_rms", 0.012))
        silence_flush = int(self.cfg.get("silence_flush_ms", 1000))
        max_seg = int(self.cfg.get("max_segment_ms", 15000))

        # 门限跟随环境噪声，安静房间能听到更远的人，嘈杂房间不误触发
        gate = max(gate_min, self.noise * float(self.cfg.get("vad_noise_mult", 2.2)))
        voiced = level >= gate
        if not voiced:
            self._silent_frames += 1
            if self._silent_frames > int(12000 / FRAME_MS) and not self._no_voice_hint:
                self._no_voice_hint = True
                self.on_error("已经 12 秒没有检测到语音：确认麦克风/系统声来源，或把「灵敏度」调到高")
        else:
            self._silent_frames = 0
        if not voiced and not self.in_speech:
            self.noise = min(gate_min, self.noise * 0.98 + level * 0.02)

        if voiced:
            self.voiced_run += 1
            if not self.in_speech and self.voiced_run >= 3:
                self.in_speech = True
                self.seg = bytearray(b"".join(self.preroll))
                self.seg_start_ms = max(0, now_ms - len(self.preroll) * FRAME_MS)
                self.preroll.clear()
            if self.in_speech:
                self.seg += frame
                self.silence_ms = 0
                if len(self.seg) * 1000 / (RATE * 2) >= max_seg:
                    self._flush()
        else:
            self.voiced_run = 0
            self.preroll.append(frame)
            if self.in_speech:
                self.seg += frame
                self.silence_ms += FRAME_MS
                if self.silence_ms >= silence_flush:
                    self._flush()

    def _flush(self, final: bool = False):
        if not self.seg:
            return
        pcm = bytes(self.seg)
        dur_ms = len(pcm) * 1000 // (RATE * 2)
        # 去掉尾部静音，避免 whisper 幻觉
        trim = min(self.silence_ms, 400) * RATE * 2 // 1000
        if trim and len(pcm) > trim:
            pcm = pcm[: len(pcm) - (trim // 2 * 2)]
            dur_ms = len(pcm) * 1000 // (RATE * 2)
        pcm = _normalize(pcm)
        start_ms = self.seg_start_ms
        self.seg = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        if dur_ms < int(self.cfg.get("min_segment_ms", 700)):
            return
        try:
            self.on_segment(start_ms, start_ms + dur_ms, pcm)
        except Exception as e:
            self.on_error(f"segment callback: {e}")

    # ------------- 控制 -------------
    def start(self, mic_index: int | None, source: str = "mic"):
        self.names = []
        if platforms.NATIVE_CAPTURE:
            # Windows：loopcap 一个进程搞定 mic/system/mix，内部混音
            cap = platforms.system_capture_bin()
            if not cap.is_file():
                raise RuntimeError(f"采集组件缺失（{cap.name}）")
            p = _Proc("mix", platforms.native_capture_argv(RATE, source, mic_index))
            self.procs.append(p)
            self.names.append("mix")
            self.running = True
            p.start(self._feed, self.on_error)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.log(f"[capture] 开始 source={source} native={cap.name}")
            return
        ffmpeg = self.cfg.get("ffmpeg", "ffmpeg")
        if source in ("mic", "mix") and mic_index is None:
            mic_index = 0                    # 未指定时用系统默认输入
        if source in ("mic", "mix") and mic_index is not None:
            mic = _Proc("mic", platforms.mic_input_argv(ffmpeg, mic_index, RATE))
            self.procs.append(mic)
            self.names.append("mic")
        if source in ("system", "mix"):
            cap = platforms.system_capture_bin()
            if not cap.is_file():
                self.on_error(f"系统声音采集组件缺失（{cap.name}），已仅使用麦克风")
            elif not system_audio_allowed():
                # 关键：不要直接抛错就完事。这台机器上「系统设置 → 屏幕录制」里
                # 那一条很可能是上一次构建的旧记录（ad-hoc 签名每次重建都换指纹），
                # 勾着却无效。此时主动申请一次，系统才会为当前二进制弹出授权框。
                platforms.request_system_audio()
                raise RuntimeError("NEED_SCREEN_PERMISSION")
            else:
                sysp = _Proc("system", platforms.system_capture_argv(RATE))
                self.procs.append(sysp)
                self.names.append("system")
        if not self.procs:
            raise RuntimeError("没有可用的音频输入源（麦克风需在 系统设置 → 隐私与安全性 → 麦克风 中允许 MeetLite）")

        self.running = True
        for p in self.procs:
            p.start(self._feed, self.on_error)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.log(f"[capture] 开始 source={source} devices={self.names}")

    def stop(self):
        self.running = False
        for p in self.procs:
            p.stop()
        if self.thread:
            self.thread.join(timeout=3)
        self.procs = []
        self.log("[capture] 停止")