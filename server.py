#!/usr/bin/env python3
"""MeetLite 服务：录音 → 转写 → 摘要，单进程、零第三方依赖。

启动：python3 server.py [--port 8787] [--no-warm]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# embeddable Python 带 *_pth 时是隔离模式，脚本所在目录不会自动进 sys.path
# （Windows 包里靠 build-win.ps1 往 _pth 补 ..\app，这里再加一道，免得换了打包方式又踩）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import audiodec
import config as config_mod
import feedback as feedback_mod
import platforms
import store as store_mod
import model_manager
import updater
import summarize as summarize_mod
import trash as trash_mod
from capture import Recorder, list_input_devices, system_audio_allowed
import capture as capture_mod
from llm import LocalLLM
from whisper_engine import WhisperEngine

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


# --------------------------------------------------------------------------- App
# 采集灵敏度：vad_rms 门限 + 噪声自适应倍数
SENSITIVITY = {
    "low":    (0.020, 2.6),
    "normal": (0.012, 2.2),
    "high":   (0.006, 1.8),
}


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self._log_ring: list[str] = []
        self.store = store_mod.Store(config_mod.DATA_DIR / "meetlite.sqlite")
        self.whisper = WhisperEngine(cfg, self.log)
        self.llm = LocalLLM(cfg, self.log)
        self.summarizer = summarize_mod.Summarizer(self.llm, self.log)

        self.recorder: Recorder | None = None
        self.rec_meeting: str | None = None
        self.rec_started: float = 0.0
        self.level: float = 0.0
        self.time_offset: int = 0
        self.level_tick = 0
        self.warm_state = "idle"
        self._perm_at = 0.0
        self._perm_ok = None
        self._perm_busy = False
        self._devices = []
        self._devices_at = 0.0
        self._devices_busy = False

        # ---- 回收站 ----
        self._trash_seen = 0.0
        try:
            n = len(trash_mod.auto_purge(self.trash_days()))
            if n:
                self.log(f"回收站：清理了 {n} 条过期记录")
        except Exception as e:
            self.log(f"回收站清理失败：{e}")
        threading.Thread(target=self._trash_tick, daemon=True).start()

        self.jobs: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self.worker.start()

        self._subs: dict[str, list[queue.Queue]] = {}
        self._subs_lock = threading.Lock()

        try:
            n = self.store.reset_recording_states()
            if n:
                self.log(f"[store] 清理 {n} 条残留录制状态")
        except Exception as e:
            self.log(f"[store] 清理残留状态失败: {e}")

        self.busy = {"summarize": False, "import": False}
        self.last_text: dict[str, str] = {}   # meeting_id -> 最近一段，用作 initial_prompt
        self.last_error: str | None = None

    # ---------------- 回收站 ----------------
    def trash_enabled(self) -> bool:
        v = self.cfg.get("trash_enabled")
        return True if v is None else bool(v)

    def trash_days(self) -> int:
        try:
            d = int(self.cfg.get("trash_days") or 7)
        except (TypeError, ValueError):
            d = 7
        return max(1, min(d, 365))

    def trash_move(self, mid: str) -> dict:
        r = trash_mod.move_to_trash(self.store, mid)
        if r.get("ok"):
            self.log(f"回收站：{r.get('title')} → {r.get('entry')}")
        return r

    def _trash_tick(self):
        """每小时清一次过期项（App 长期开着也不会堆着）。"""
        while True:
            time.sleep(3600)
            try:
                n = len(trash_mod.auto_purge(self.trash_days()))
                if n:
                    self.log(f"回收站：清理了 {n} 条过期记录")
            except Exception:
                pass

    def system_audio_ok(self):
        """不阻塞：探测在后台线程做，先返回缓存值（未知返回 None）。"""
        import time as _t
        now = _t.time()
        stale = now - self._perm_at > 20
        if stale and not self._perm_busy:
            self._perm_busy = True

            def probe():
                try:
                    self._perm_ok = system_audio_allowed()
                except Exception:
                    self._perm_ok = None
                finally:
                    self._perm_at = _t.time()
                    self._perm_busy = False

            threading.Thread(target=probe, daemon=True).start()
        return self._perm_ok

    def devices(self):
        """设备列表：缓存 60s，后台刷新，不阻塞请求。

        首次调用例外：同步探测一次（loopcap --list / ffmpeg 列设备都是毫秒级），
        否则首屏拿到的永远是空数组，采集源下拉框会是空的。
        """
        import time as _t
        now = _t.time()
        if self._devices and now - self._devices_at < 60:
            return self._devices
        if not self._devices and not self._devices_busy:
            self._devices_busy = True
            try:
                self._devices = list_input_devices(self.cfg.get("ffmpeg", "ffmpeg"))
            except Exception:
                pass
            finally:
                self._devices_at = _t.time()
                self._devices_busy = False
            if self._devices:
                return self._devices
        if not self._devices_busy:
            self._devices_busy = True

            def probe():
                try:
                    self._devices = list_input_devices(self.cfg.get("ffmpeg", "ffmpeg"))
                except Exception:
                    pass
                finally:
                    self._devices_at = _t.time()
                    self._devices_busy = False

            threading.Thread(target=probe, daemon=True).start()
        return self._devices

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + str(msg)
        self._log_ring.append(line)
        if len(self._log_ring) > 200:
            del self._log_ring[:len(self._log_ring) - 200]
        print(line, flush=True)

    def log_tail(self, n=30) -> list[str]:
        return self._log_ring[-n:]

    # ---------------- 事件广播 ----------------
    def subscribe(self, mid: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._subs_lock:
            self._subs.setdefault(mid, []).append(q)
        return q

    def unsubscribe(self, mid: str, q: queue.Queue):
        with self._subs_lock:
            lst = self._subs.get(mid, [])
            if q in lst:
                lst.remove(q)

    def broadcast(self, mid: str, event: dict):
        with self._subs_lock:
            subs = list(self._subs.get(mid, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ---------------- 转写流水线 ----------------
    def _transcribe_loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            kind = job[0]
            try:
                if kind == "segment":
                    _, mid, start_ms, end_ms, pcm = job
                    self._do_segment(mid, start_ms, end_ms, pcm)
                elif kind == "chunk":
                    _, mid, start_ms, end_ms, pcm, total, done = job
                    self._do_segment(mid, start_ms, end_ms, pcm, progress=(total, done))
            except Exception as e:
                self.last_error = str(e)
                self.log(f"[worker] 失败: {e}\n{traceback.format_exc()}")
                if job[1]:
                    self.broadcast(job[1], {"type": "error", "message": str(e)})

    def _do_segment(self, mid, start_ms, end_ms, pcm, progress=None):
        lang = (self.cfg.get("asr_lang") or "auto").strip().lower() or "auto"
        t0 = time.time()
        hint = config_mod.asr_hint(self.cfg)
        tail = self.last_text.get(mid, "")[-180:]
        prompt = (hint + " " + tail).strip() if hint else tail
        text = self.whisper.transcribe(pcm, language=lang, prompt=prompt)
        dt = time.time() - t0
        if progress:
            total, done = progress
            self.broadcast(mid, {"type": "progress", "phase": "import",
                                 "text": f"转写 {done}/{total}", "ratio": done / total})
        if not text:
            return
        seg = self.store.add_segment(mid, start_ms, end_ms, text, lang)
        self.last_text[mid] = (self.last_text.get(mid, "") + " " + text)[-400:]
        self.log(f"[asr] {store_mod.fmt_ts(start_ms)} {dt:.1f}s {text[:60]}")
        self.broadcast(mid, {"type": "segment", "segment": seg})

    # ---------------- 录音 ----------------
    def start_recording(self, mic_index, source="mix", meeting_id=None, title=None, sensitivity=None):
        if self.recorder:
            raise RuntimeError("已经在录音")
        mid = meeting_id or self.store.create_meeting(title)["id"]
        if not self.store.get_meeting(mid):
            raise RuntimeError("会议不存在")
        self.store.update_meeting(mid, status="recording")
        self.rec_meeting = mid
        self.rec_started = time.time()
        # 续录时把时间轴接到已有片段之后
        self.time_offset = int((self.store.get_meeting(mid) or {}).get("duration_ms") or 0)

        def on_level(level):
            self.level = level
            self.level_tick += 1
            if self.level_tick % 2 == 0:
                self.broadcast(mid, {"type": "level", "value": round(level, 4)})

        off = self.time_offset

        def on_segment(start_ms, end_ms, pcm):
            self.jobs.put(("segment", mid, start_ms + off, end_ms + off, pcm))

        rcfg = dict(self.cfg)
        if sensitivity in SENSITIVITY:
            rcfg["vad_rms"], rcfg["vad_noise_mult"] = SENSITIVITY[sensitivity]
        rec = Recorder(rcfg, on_segment, on_level=on_level,
                       on_error=lambda m: self.on_rec_error(mid, m), log=self.log)
        rec.start(mic_index, source)
        self.recorder = rec
        self.broadcast(mid, {"type": "state", "state": "recording", "meeting_id": mid})
        return mid

    def on_rec_error(self, mid, message):
        self.last_error = message
        self.broadcast(mid, {"type": "error", "message": message})

    def stop_recording(self):
        mid = self.rec_meeting
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        self.rec_meeting = None
        if mid:
            self.store.update_meeting(mid, status="done")
            self.broadcast(mid, {"type": "state", "state": "stopped", "meeting_id": mid})
            self.broadcast(mid, {"type": "notice", "message": "录音已结束，剩余的转写完成后可生成纪要"})
        return mid

    # ---------------- 导入音频 ----------------
    def import_audio(self, mid, path, name=""):
        if self.busy["import"]:
            raise RuntimeError("已有导入任务在进行")
        self.busy["import"] = True
        threading.Thread(target=self._import_worker, args=(mid, path, name), daemon=True).start()

    def _import_worker(self, mid, path, name):
        try:
            self.broadcast(mid, {"type": "progress", "phase": "import", "text": "解码音频…", "ratio": 0})
            # wav 走标准库，其它格式交给 ffmpeg（Windows 包默认不带 ffmpeg）
            pcm = audiodec.to_pcm16k(
                path,
                on_note=lambda msg: self.broadcast(mid, {"type": "notice", "message": msg}),
            )
            # 按静音切句，而不是定长 30 秒：whisper 一次解码只有一种语言标签，
            # 30 秒窗口里中英混说时非目标语言会被整段丢掉；按句子切之后每段基本
            # 是单一语言，language=auto 才判得准。
            segs = capture_mod.segment_pcm(
                pcm,
                max_ms=int(self.cfg.get("import_max_segment_ms", 12000)),
                silence_ms=int(self.cfg.get("import_silence_ms", 600)),
                gate_min=float(self.cfg.get("vad_rms", 0.012)),
            )
            total = len(segs)
            self.broadcast(mid, {"type": "progress", "phase": "import",
                                 "text": f"共 {total} 句，开始转写", "ratio": 0})
            for i, (start_ms, end_ms, ch) in enumerate(segs, 1):
                self.jobs.put(("chunk", mid, start_ms, end_ms, ch, total, i))
            self.jobs.put(("segment", mid, 0, 0, b""))  # 用空段作为队列哨兵
            self.broadcast(mid, {"type": "notice", "message": f"已提交 {total} 段转写"})
        except Exception as e:
            self.last_error = str(e)
            self.broadcast(mid, {"type": "error", "message": f"导入失败: {e}"})
        finally:
            self.busy["import"] = False
            try:
                os.unlink(path)
            except OSError:
                pass


APP: App | None = None


# ----------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    protocol_version = "HTTP/1.1"
    server_version = "MeetLite/1.0"

    # ---- 工具 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _read_body_once(self):
        """入口处读一次请求体并缓存。

        不读的话，body 会留在 socket 缓冲区，被 BaseHTTPRequestHandler 当成
        下一条请求行解析（日志里那句 code 400, message Bad request syntax ('{}')），
        keep-alive 连接就此报废。

        注意：HTTP/1.1 keep-alive 时同一个 handler 实例会连着处理多条请求，
        所以不能靠「已经读过就跳过」——那样第二条请求的 body 就没被读走。
        每次都按当前 Content-Length 重新读。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self._body_raw = self.rfile.read(n) if n > 0 else b""

    def _raw_body(self) -> bytes:
        # 已经在 do_* 入口读过了；这里只能取缓存，再 read 一次会卡在 socket 上
        return getattr(self, "_body_raw", b"")

    def _body(self) -> dict:
        raw = self._raw_body()
        if not raw:
            return {}
        try:
            out = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return out if isinstance(out, dict) else {}

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/events"):
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- 路由 ----
    def do_GET(self):
        self._read_body_once()          # GET 一般不带宽体，但宽体不读干净一样会污染下一条
        self._route("GET")

    def do_POST(self):
        self._read_body_once()          # 先把 body 读干净，避免污染 keep-alive
        self._route("POST")

    def do_PATCH(self):
        self._read_body_once()
        self._route("PATCH")

    def do_DELETE(self):
        self._read_body_once()
        self._route("DELETE")

    def _route(self, method):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if method == "GET" and (p == "/" or p == "/index.html"):
                return self._static("index.html")
            if method == "GET" and p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if method == "GET" and p == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if p.startswith("/api/"):
                return self._api(method, p, qs)
            return self._err("not found", 404)
        except BrokenPipeError:
            return None
        except Exception as e:
            self.log_error(traceback.format_exc())
            try:
                return self._err(str(e), 500)
            except Exception:
                return None

    def _static(self, rel):
        rel = rel.replace("..", "")
        path = os.path.join(STATIC, rel)
        if not os.path.isfile(path):
            return self._err("not found", 404)
        ct = "text/html; charset=utf-8"
        if rel.endswith(".js"):
            ct = "application/javascript; charset=utf-8"
        elif rel.endswith(".css"):
            ct = "text/css; charset=utf-8"
        elif rel.endswith(".svg"):
            ct = "image/svg+xml"
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _api(self, method, p, qs):
        app = APP
        assert app

        # ---- 系统信息 ----
        if method == "GET" and p == "/api/system":
            cfg = app.cfg
            return self._json({
                "ok": True,
                "version": config_mod.APP_VERSION,
                "models_ready": bool(cfg.get("whisper_model") and cfg.get("summary_model")),
                "models": {
                    "whisper": os.path.basename(cfg.get("whisper_model") or ""),
                    "summary": os.path.basename(cfg.get("summary_model") or ""),
                    "engine": cfg.get("llama_server"),
                },
                "devices": app.devices(),
                "templates": summarize_mod.list_templates(cfg.get("ui_lang")),
                "language": cfg.get("asr_lang"),
                "ui_lang": cfg.get("ui_lang") or "zh",
                "asr_lang": cfg.get("asr_lang") or "auto",
                "asr_langs": config_mod.ASR_LANGS,
                "recording": bool(app.recorder),
                "warm": app.warm_state,
                "system_audio": app.system_audio_ok(),
                "recording_meeting": app.rec_meeting,
                "busy": app.busy,
                "last_error": app.last_error,
            })

        # ---- SSE ----
        # ---- 模板 ----
        if method == "GET" and p == "/api/templates":
            lang = (qs.get("lang") or [app.cfg.get("ui_lang") or "zh"])[0]
            return self._json({"ok": True, "lang": lang, "templates": summarize_mod.list_templates(lang)})

        # ---- 界面偏好（端口每次启动会变，localStorage 不可靠，存到 config.json）----
        # ---- 问题反馈 ----
        if p == "/api/feedback/diagnostics" and method == "GET":
            diag = feedback_mod.diagnostics(app.cfg, app)
            return self._json({"ok": True, "diagnostics": diag,
                               "text": feedback_mod.diag_text(diag),
                               "webhook": bool((app.cfg.get("feedback_webhook") or "").strip())})

        if p == "/api/feedback" and method == "POST":
            body = self._body()
            if not isinstance(body, dict):
                return self._json({"ok": False, "error": "bad body"}, 400)
            res = feedback_mod.submit(app.cfg, app, body)
            return self._json(res, 200 if res.get("ok") else 200)

        if p == "/api/feedback" and method == "GET":
            return self._json({"ok": True, "items": feedback_mod.local_list(20)})

        if p == "/api/prefs":
            if method == "GET":
                return self._json({
                    "ok": True,
                    "theme": app.cfg.get("theme") or "auto",
                    "perm_dismissed": bool(app.cfg.get("perm_dismissed")),
                    "ui_lang": app.cfg.get("ui_lang") or "zh",
                    "asr_lang": app.cfg.get("asr_lang") or "auto",
                    "close_action": app.cfg.get("close_action") or "ask",
                })
            if method == "POST":
                body = self._body()
                if not isinstance(body, dict):
                    return self._json({"ok": False, "error": "bad body"}, 400)
                if "theme" in body and body["theme"] in ("light", "dark", "auto"):
                    app.cfg["theme"] = body["theme"]
                if "perm_dismissed" in body:
                    app.cfg["perm_dismissed"] = bool(body["perm_dismissed"])
                if body.get("ui_lang") in ("zh", "en"):
                    app.cfg["ui_lang"] = body["ui_lang"]
                if isinstance(body.get("asr_lang"), str) and body["asr_lang"].strip():
                    app.cfg["asr_lang"] = body["asr_lang"].strip().lower()
                if body.get("close_action") in ("ask", "background", "quit"):
                    app.cfg["close_action"] = body["close_action"]
                app.cfg.save()
                return self._json({"ok": True})

        # ---- 自更新 ----
        if p == "/api/update/check" and method == "GET":
            url = (qs.get("url") or [""])[0].strip()
            res = updater.check(url or None)
            res["version"] = updater.current()
            res["log"] = str(updater.UPDATE_DIR / "apply.log")
            return self._json(res)

        if p == "/api/update/progress" and method == "GET":
            res = dict(updater.PROGRESS)
            res["ok"] = True          # 前端 api() 要求 ok 字段，否则整条轮询会被当成失败
            return self._json(res)

        if p == "/api/update/download" and method == "POST":
            body = self._body() or {}
            if updater.PROGRESS.get("running"):
                return self._json({"ok": False, "error": "已有更新任务在进行"}, 409)
            if isinstance(body, dict) and body.get("url"):
                app.cfg["update_url"] = str(body["url"]).strip()
                app.cfg.save()
            res = updater.download_and_stage()
            return self._json(res, 200 if res.get("ok") else 500)

        if p == "/api/update/apply" and method == "POST":
            body = self._body() or {}
            staged = (body or {}).get("staged") or updater.PROGRESS.get("staged") or ""
            if not staged:
                return self._json({"ok": False, "error": "没有待安装的更新"}, 400)
            res = updater.apply_staged(staged)
            if res.get("ok"):
                threading.Thread(target=_quit_soon, args=(app,), daemon=True).start()
            return self._json(res)

        # ---- 模型管理 ----
        if p == "/api/models" and method == "GET":
            return self._json({
                "ok": True,
                "models": model_manager.catalog(),
                "asr_model": os.path.basename(str(app.cfg.get("whisper_model") or "")),
                "llm_model": os.path.basename(str(app.cfg.get("summary_model") or "")),
                "dir": str(config_mod.DATA_DIR),
            })

        if p == "/api/models/progress" and method == "GET":
            return self._json(dict(model_manager.PROGRESS))

        if p == "/api/models/download" and method == "POST":
            body = self._body()
            if not isinstance(body, dict) or not body.get("id"):
                return self._json({"ok": False, "error": "缺少 id"}, 400)
            if model_manager.PROGRESS.get("running"):
                return self._json({"ok": False, "error": "已有下载在进行"}, 409)
            mid, url = body["id"], (body.get("url") or "").strip()
            return self._model_job(lambda cb: model_manager.install(mid, cb, url), "下载 " + mid)

        if p == "/api/models/upload" and method == "POST":
            name = (qs.get("name") or ["model.bin"])[0]
            name = os.path.basename(name) or "model.bin"
            kind = (qs.get("kind") or [""])[0]
            raw = self._raw_body()
            if not raw:
                return self._json({"ok": False, "error": "空文件"}, 400)
            tmp = os.path.join(tempfile.mkdtemp(prefix="meetlite-up-"), name)
            with open(tmp, "wb") as f:
                f.write(raw)
            return self._model_job(lambda cb: model_manager.import_local(tmp, kind, cb), "导入 " + name)

        if p == "/api/models/use" and method == "POST":
            body = self._body()
            item = model_manager.find((body or {}).get("id", "")) if isinstance(body, dict) else None
            if not item:
                return self._json({"ok": False, "error": "未知模型"}, 400)
            path = model_manager.installed_path(item)
            if not path:
                return self._json({"ok": False, "error": "该模型尚未安装"}, 400)
            if item.get("kind") == "whisper":
                app.cfg["whisper_model"] = path
                try:
                    app.whisper.stop()
                except Exception as e:
                    app.log(f"[models] 停 whisper 失败 {e}")
            else:
                app.cfg["summary_model"] = path
            app.cfg.save()
            return self._json({"ok": True, "path": path,
                               "restart": item.get("kind") != "whisper"})

        if method == "GET" and p == "/api/events":
            mid = (qs.get("meeting_id") or [""])[0]
            return self._sse(mid)

        # ---- 会议 ----
        if p == "/api/meetings":
            if method == "GET":
                return self._json({"ok": True, "meetings": app.store.list_meetings()})
            if method == "POST":
                b = self._body()
                m = app.store.create_meeting(b.get("title"))
                return self._json({"ok": True, "meeting": m})

        m = re.fullmatch(r"/api/meetings/([0-9a-zA-Z]+)", p)
        if m:
            mid = m.group(1)
            if method == "GET":
                mt = app.store.get_meeting(mid)
                if not mt:
                    return self._err("会议不存在", 404)
                return self._json({
                    "ok": True, "meeting": mt,
                    "segments": app.store.segments(mid),
                    "summary": app.store.get_summary(mid),
                    "notes": app.store.get_notes(mid),
                })
            if method == "DELETE":
                if app.recorder and app.rec_meeting == mid:
                    return self._err("正在录音，先停止录音", 409)
                if app.trash_enabled():
                    r = app.trash_move(mid)
                    if not r.get("ok"):
                        return self._err(r.get("error") or "删除失败", 404)
                    return self._json({"ok": True, "trash": r.get("entry"),
                                       "title": r.get("title"),
                                       "days": app.trash_days()})
                app.store.delete_meeting(mid)
                return self._json({"ok": True})
            if method == "PATCH":
                b = self._body()
                fields = {k: b[k] for k in ("title", "status") if k in b}
                if fields:
                    app.store.update_meeting(mid, **fields)
                if "notes" in b:
                    app.store.save_notes(mid, b["notes"])
                return self._json({"ok": True, "meeting": app.store.get_meeting(mid)})

        # ---- 回收站 ----
        if p == "/api/trash":
            if method == "GET":
                return self._json({"ok": True, "items": trash_mod.list_items(app.trash_days()),
                                   "days": app.trash_days(),
                                   "enabled": app.trash_enabled()})
            if method == "DELETE":
                return self._json(trash_mod.empty())

        m = re.fullmatch(r"/api/trash/([^/]+)", p)
        if m:
            name = m.group(1)
            if method == "DELETE":
                return self._json(trash_mod.purge(name))
            if method == "GET":
                d = trash_mod.get_item(name)
                if not d:
                    return self._err("回收站里没有这一项", 404)
                return self._json({"ok": True, "item": d})

        m = re.fullmatch(r"/api/trash/([^/]+)/restore", p)
        if m and method == "POST":
            r = trash_mod.restore(app.store, m.group(1))
            if r.get("ok"):
                app.log(f"回收站：恢复会议 {r.get('meeting_id')}")
            return self._json(r) if r.get("ok") else self._err(r.get("error") or "恢复失败", 404)

        # ---- 录音控制 ----
        if p == "/api/record/start" and method == "POST":
            b = self._body()
            try:
                mid = app.start_recording(
                    b.get("mic_index"), b.get("source", "mix"),
                    b.get("meeting_id"), b.get("title"), b.get("sensitivity"),
                )
                return self._json({"ok": True, "meeting_id": mid})
            except Exception as e:
                if "NEED_SCREEN_PERMISSION" in str(e):
                    return self._json({"ok": False, "code": "need_screen_permission",
                                       "error": "需要屏幕录制权限：系统已弹出授权框，允许后请重新打开 MeetLite（设置里那条旧勾选可能是上一次构建留下的，重新勾一次即可）"}, 403)
                return self._err(e)

        if p == "/api/open-privacy" and method == "POST":
            b = self._body()
            kind = b.get("kind") or "screen"
            pane = {"screen": "Privacy_ScreenCapture", "mic": "Privacy_Microphone"}.get(kind, "Privacy_ScreenCapture")
            # 先申请一次：只有真正调用 CGRequestScreenCaptureAccess()，系统才会
            # 为当前这份二进制登记授权并弹窗。光跳转到设置页，用户看到的是上一
            # 次构建留下的旧记录（ad-hoc 签名换了指纹），勾着也照样录不到。
            granted = False
            if kind == "screen":
                try:
                    granted = platforms.request_system_audio()
                except Exception:
                    granted = False
                app._perm_at = 0            # 让 /api/system 立刻重新探测
            try:
                subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
                               check=False, timeout=10)
                return self._json({"ok": True, "granted": granted})
            except Exception as e:
                return self._err(e)

        if p == "/api/record/stop" and method == "POST":
            mid = app.stop_recording()
            return self._json({"ok": True, "meeting_id": mid})

        # ---- 摘要 ----
        m = re.fullmatch(r"/api/meetings/([0-9a-zA-Z]+)/summarize", p)
        if m and method == "POST":
            mid = m.group(1)
            b = self._body()
            tpl = b.get("template") or "standard_meeting"
            return self._summarize_async(mid, tpl)

        # ---- 导入 ----
        if p == "/api/import" and method == "POST":
            mid = (qs.get("meeting_id") or [""])[0]
            name = (qs.get("name") or ["audio"])[0]
            data = self._raw_body()
            if not data:
                return self._err("空文件")
            if not mid:
                mid = app.store.create_meeting(f"导入 {name[:40]}")["id"]
            tmp = tempfile.mktemp(suffix=os.path.splitext(name)[1] or ".bin")
            with open(tmp, "wb") as f:
                f.write(data)
            app.import_audio(mid, tmp, name)
            return self._json({"ok": True, "meeting_id": mid})

        # ---- 导出 ----
        m = re.fullmatch(r"/api/meetings/([0-9a-zA-Z]+)/export", p)
        if m and method == "GET":
            mid = m.group(1)
            mt = app.store.get_meeting(mid)
            if not mt:
                return self._err("会议不存在", 404)
            md = self._markdown(mid)
            body = md.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"meeting-{mid}.md\"")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None

        return self._err("not found", 404)

    def _markdown(self, mid) -> str:
        app = APP
        mt = app.store.get_meeting(mid) or {}
        segs = app.store.segments(mid)
        sm = app.store.get_summary(mid)
        notes = app.store.get_notes(mid)
        out = [f"# {mt.get('title', '会议')}", ""]
        out.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(mt.get('created_at', time.time())))}")
        out.append(f"- 时长：{store_mod.fmt_ts(mt.get('duration_ms', 0))}")
        out.append(f"- 片段：{len(segs)}")
        out.append("")
        if sm:
            out.append("## 会议纪要")
            out.append("")
            out.append(sm["content"])
            out.append("")
        if notes.strip():
            out.append("## 我的笔记")
            out.append("")
            out.append(notes)
            out.append("")
        out.append("## 完整转写")
        out.append("")
        for s in segs:
            out.append(f"**[{store_mod.fmt_ts(s['start_ms'])}]** {s['text']}")
            out.append("")
        return "\n".join(out)

    def _model_job(self, fn, label):
        """模型下载/导入统一走后台线程 + 全局进度。"""
        app = APP
        model_manager.PROGRESS.update({"running": True, "id": label, "done": 0, "total": 0,
                                       "phase": "准备中", "error": "", "message": ""})

        def run():
            def cb(done, total, phase):
                model_manager.PROGRESS.update({"done": int(done), "total": int(total),
                                               "phase": phase})
            try:
                app.log(f"[models] {label} 开始")
                res = fn(cb)
                model_manager.PROGRESS.update({"running": False, "phase": "完成",
                                               "done": 1, "total": 1, "error": "",
                                               "message": str((res or {}).get("path") or "")})
                app.log(f"[models] {label} 完成 {res}")
            except Exception as e:
                model_manager.PROGRESS.update({"running": False, "phase": "失败", "error": str(e)})
                app.log(f"[models] {label} 失败 {e}")
        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "status": "started"})

    def _summarize_async(self, mid, tpl):
        app = APP
        if app.busy["summarize"]:
            return self._err("已有一个摘要任务在进行")
        text = app.store.transcript_text(mid)
        if not text.strip():
            return self._err("还没有转写内容")

        app.busy["summarize"] = True

        def run():
            t0 = time.time()
            try:
                app.broadcast(mid, {"type": "progress", "phase": "summary", "text": "整理中…", "ratio": 0})
                content = app.summarizer.summarize(
                    text, tpl, lang=app.cfg.get("ui_lang"),
                    progress=lambda msg: app.broadcast(mid, {"type": "progress", "phase": "summary", "text": msg}),
                )
                app.store.save_summary(mid, tpl, content)
                dt = time.time() - t0
                app.log(f"[summary] {mid} 完成 {dt:.1f}s")
                app.broadcast(mid, {"type": "summary", "template": tpl, "content": content,
                                    "elapsed": round(dt, 1)})
            except Exception as e:
                app.last_error = str(e)
                app.log(f"[summary] 失败 {e}\n{traceback.format_exc()}")
                app.broadcast(mid, {"type": "error", "message": f"摘要失败: {e}"})
            finally:
                app.busy["summarize"] = False

        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "status": "started"})

    def _sse(self, mid: str):
        app = APP
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        q = app.subscribe(mid)

        def send(obj):
            payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode())
            self.wfile.flush()

        try:
            send({"type": "hello", "state": "recording" if app.recorder else "idle",
                  "meeting_id": app.rec_meeting, "busy": app.busy})
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                send(ev)
        except Exception:
            pass
        finally:
            app.unsubscribe(mid, q)
        return None


# ------------------------------------------------------------------------ main
def _quit_soon(app):
    """给响应留出发送时间，再关掉服务，让替换脚本接手。"""
    import time
    time.sleep(0.8)
    app.log("[update] 退出以安装更新")
    try:
        app.whisper.stop()
    except Exception:
        pass
    try:
        getattr(app.llm, "stop", lambda: None)()
    except Exception:
        pass
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-warm", action="store_true", help="不预热模型")
    ap.add_argument("--source", default=None, help="默认采集源 mic|system|mix")
    ap.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开界面")
    args = ap.parse_args()

    cfg = config_mod.load()
    # --port 0 = 让系统分配空闲端口，所以不能用 `or` 兜底（0 是 falsy）
    port = args.port if args.port is not None else int(cfg.get("port", 8787))
    try:
        updater.cleanup()          # 清掉上次更新留下的 .old / 脚本 / 解包目录
    except Exception:
        pass

    # 缺模型不再退出：Windows 包首次运行 100% 没有模型，界面上的「先准备两个本地模型」
    # 向导必须能渲染出来。这里只提示，预热线程失败也不影响起服务。
    if not cfg.get("whisper_model"):
        print("尚未配置 whisper 模型：可在界面里下载，或在 config.json 指定 whisper_model")
    if not cfg.get("summary_model"):
        print("尚未配置摘要模型（.gguf）：可在界面里下载，或在 config.json 指定 summary_model")
    if not cfg.get("llama_server"):
        print("找不到 llama-server：macOS 用 brew install llama.cpp，或把可执行文件放进 bin/ 目录")

    global APP
    APP = App(cfg)

    def _name(v):
        return os.path.basename(str(v)) if v else "未配置"

    # 先建 socket 再打 banner：--port 0 时真实端口由系统分配，得从 server_address 取
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"端口 {port} 起不来：{e}")
        print("可以换一个端口试试：python server.py --port 0")
        raise SystemExit(1)
    srv.daemon_threads = True
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}"

    print("=" * 62)
    print(" MeetLite")
    print(f" 转写  localWhisper / {_name(cfg.get('whisper_model'))}")
    print(f" 摘要  builtin-ai  / {_name(cfg.get('summary_model'))}")
    print(f" 界面  {url}")
    print("=" * 62)

    if args.open:
        def _open_browser():
            time.sleep(0.5)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open_browser, daemon=True).start()

    if not args.no_warm:
        threading.Thread(target=_warm, args=(APP,), daemon=True).start()

    # 父进程（MeetLite.app 的 Swift 壳）退出后自身也退出，避免留下孤儿服务
    def _watch_parent():
        ppid0 = os.getppid()
        while True:
            time.sleep(2)
            if not platforms.parent_alive(ppid0):
                print("[server] 父进程已退出，自行关闭")
                try:
                    if APP.recorder:
                        APP.stop_recording()
                    APP.whisper.stop()
                    APP.llm.close()
                except Exception:
                    pass
                os._exit(0)

    threading.Thread(target=_watch_parent, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出中…")
    finally:
        srv.shutdown()
        try:
            if APP.recorder:
                APP.stop_recording()
            APP.whisper.stop()
            APP.llm.close()
        except Exception:
            pass


def _warm(app: App):
    app.warm_state = "loading"
    try:
        app.whisper.warmup()
        app.llm.warmup()
        app.warm_state = "ready"
        app.log("[warm] 模型已就绪")
    except Exception as e:
        app.warm_state = "failed"
        app.log(f"[warm] 预热失败（不影响使用）: {e}")


if __name__ == "__main__":
    main()