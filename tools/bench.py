#!/usr/bin/env python3
"""基准测试：ASR 实时率 + 纪要生成耗时。mac / Windows 通用。

    python3 tools/bench.py                       # 用内置样本（若存在）
    python3 tools/bench.py --wav 会议.wav --repeat 3
    python3 tools/bench.py --tpl standard_meeting --lang zh

输出每轮明细与均值，末行给出 RTF（识别耗时 / 音频时长）与「一小时会议预计耗时」。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import subprocess  # noqa: E402

import config as config_mod  # noqa: E402
import summarize as summarize_mod  # noqa: E402
from llm import LocalLLM  # noqa: E402
from whisper_engine import WhisperEngine  # noqa: E402


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def to_pcm16(path: Path, sample_rate: int = 16000) -> bytes:
    """任意音频 → 16 kHz 单声道 s16le 裸流（与录音链路一致）。"""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sample_rate),
         "-f", "s16le", "-"],
        capture_output=True, check=True,
    )
    return out.stdout


def find_sample() -> Path | None:
    for c in (ROOT / "samples" / "sample.wav", Path("/tmp/meeting_zh.wav"), Path("/tmp/bench.wav")):
        if c.exists():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", type=Path, default=None)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--tpl", default="standard_meeting")
    ap.add_argument("--lang", default=None, help="zh / en，默认跟 config 的 ui_lang")
    ap.add_argument("--skip-summary", action="store_true")
    args = ap.parse_args()

    wav = args.wav or find_sample()
    if not wav or not wav.exists():
        print("找不到测试音频，用 --wav 指定一个 16 kHz 单声道 wav（或任意 wav，会自动重采样）")
        return 2

    cfg = config_mod.load()
    lang = args.lang or (cfg.get("ui_lang") or "zh")
    dur = wav_duration(wav)

    def log(msg: str) -> None:
        print(msg, flush=True)

    whisper = WhisperEngine(cfg, log)
    llm = LocalLLM(cfg, log)
    summ = summarize_mod.Summarizer(llm, log)

    print(f"音频      {wav.name}  {dur:.1f}s")
    print(f"ASR 模型  {Path(cfg.get('whisper_model') or '?').name}")
    print(f"LLM 模型  {Path(cfg.get('summary_model') or '?').name}")
    print(f"模板      {args.tpl} / 语言 {lang}")
    print("-" * 58)

    pcm = to_pcm16(wav)
    if not pcm:
        print("ffmpeg 转换失败")
        return 2

    t0 = time.time()
    whisper.start()
    llm.warmup()
    print(f"引擎预热  {time.time() - t0:.1f}s")

    asr_times, texts = [], []
    for i in range(max(1, args.repeat)):
        t = time.time()
        text = whisper.transcribe(pcm, language=None, prompt=config_mod.asr_hint(cfg))
        dt = time.time() - t
        asr_times.append(dt)
        texts.append(text or "")
        print(f"识别 #{i + 1}  {dt:6.2f}s  RTF {dt / dur:.2f}  {len(text or '')} 字")

    rtf = statistics.mean(asr_times) / dur
    print("-" * 58)
    print(f"识别均值  {statistics.mean(asr_times):.2f}s   RTF {rtf:.2f}   "
          f"一小时音频约 {rtf * 3600 / 60:.1f} 分钟")

    if args.skip_summary:
        return 0

    transcript = texts[0] if texts else ""
    if not transcript.strip():
        print("识别结果为空，跳过纪要测试")
        return 1

    st = time.time()
    minutes = summ.summarize(transcript, template_id=args.tpl, lang=lang,
                             progress=lambda m: print("  " + m, flush=True))
    sdt = time.time() - st
    print("-" * 58)
    print(f"纪要      {sdt:6.2f}s  {len(minutes or '')} 字  "
          f"({len(minutes or '') / max(sdt, 0.01):.0f} 字/秒)")
    print()
    print("结果摘要：")
    print((minutes or "")[:400].strip())
    print()
    print(json.dumps({
        "wav": str(wav), "duration_s": round(dur, 2),
        "asr_s": round(statistics.mean(asr_times), 2), "rtf": round(rtf, 3),
        "summary_s": round(sdt, 2), "summary_chars": len(minutes or ""),
        "lang": lang, "template": args.tpl,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())