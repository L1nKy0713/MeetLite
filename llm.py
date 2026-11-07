"""本地摘要模型：常驻 llama.cpp 的 llama-server，走 OpenAI 兼容接口。

关键点：启动时带 --jinja --reasoning-budget 0，用模型自带的 chat template
把思考链关掉。直接手拼 prompt 时 Qwen3.5 会先生成上万 token 的推理，
一份纪要约 13 分钟；关掉后同样内容 30–60 秒。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request


def strip_think(text: str) -> str:
    """兜底：万一模型仍带出推理段，剥掉它。"""
    if not text:
        return ""
    text = text.replace("\ufffd", "")
    m = re.search(r"<\s*/\s*think\s*>", text)
    if m:
        text = text[m.end():]
    text = re.sub(r"<\s*think\s*>", "", text)
    text = text.strip().lstrip("\ufeff").strip()
    if re.match(r"(?i)^(thinking process|analyze|analysis|we need|let me)", text):
        m2 = re.search(r"(?m)^\s*(#{1,4}\s|[-*]\s|\d+\.\s)", text)
        if m2:
            text = text[m2.start():]
    return text.strip()


class LocalLLM:
    """串行调用常驻 llama-server。"""

    _flag_cache: dict[tuple[str, float], set[str]] = {}

    @classmethod
    def _supported_flags(cls, exe: str) -> set[str]:
        """跑一次 `llama-server --help`，把输出里出现的开关名收集成集合。

        结果按 (路径, mtime) 缓存 —— 换版本自动失效。任何异常都退化成
        「空集合」，即只保留最核心的 -m/--host/--port 那批参数。
        """
        try:
            key = (os.path.abspath(exe), os.path.getmtime(exe))
        except OSError:
            return set()
        cached = cls._flag_cache.get(key)
        if cached is not None:
            return cached
        flags: set[str] = set()
        try:
            r = subprocess.run([exe, "--help"], capture_output=True, timeout=30)
            text = (r.stdout or b"").decode("utf-8", "replace") + \
                   (r.stderr or b"").decode("utf-8", "replace")
            flags = set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", text))
        except Exception:
            flags = set()
        cls._flag_cache[key] = flags
        return flags

    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self._proc: subprocess.Popen | None = None
        self._adopted = False
        self._lock = threading.Lock()
        self.port = int(cfg.get("llm_port") or 8402)

    # ---------- 进程管理 ----------
    def available(self) -> bool:
        return bool(self.cfg.get("llama_server")) and os.path.isfile(self.cfg.get("llama_server"))

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self, timeout=1.0) -> bool:
        try:
            with urllib.request.urlopen(self._base() + "/health", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def stop(self):
        """只停自己 spawn 的进程；复用的外部服务不动。"""
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _ensure(self):
        if self._proc and self._proc.poll() is None and self._healthy(1.0):
            return
        if self._healthy(0.8):          # 已有服务在跑，直接复用
            self._proc = None
            if not self._adopted:
                self._adopted = True
                self.log(f"[llm] 复用 127.0.0.1:{self.port} 上已在运行的服务")
            return
        model = self.cfg.get("summary_model")
        if not model or not os.path.isfile(model):
            raise RuntimeError("未找到摘要模型，请在 config.json 设置 summary_model")
        exe = self.cfg.get("llama_server")
        if not exe or not os.path.isfile(exe):
            raise RuntimeError(
                "未找到 llama-server。macOS: brew install llama.cpp；"
                "Windows: 把 llama-server.exe 放进 app\\bin\\ 目录（安装包已自带）。"
            )

        cmd = [
            exe, "-m", model,
            "--host", "127.0.0.1", "--port", str(self.port),
            "-c", str(int(self.cfg.get("llm_context") or 8192)),
            "-t", str(int(self.cfg.get("llm_threads") or 6)),
            "-ngl", "99", "-np", "1",
        ]
        # 下面这些开关只有较新的 llama.cpp 才有；老版本（例如 Windows 包用的
        # b6000）遇到未知参数会直接以 "error: invalid argument" 退出，
        # 整台机器上摘要功能就全废了。所以先问一遍 --help，只拼受支持的。
        supported = self._supported_flags(exe)
        for flag, value in (("-fa", "auto"), ("--jinja", None),
                            ("--reasoning-budget", "0"), ("--no-webui", None)):
            if flag not in supported:
                continue
            cmd.append(flag)
            if value is not None:
                cmd.append(value)
        if "--reasoning-budget" not in supported:
            # 没有该开关时靠请求体里的 chat_template_kwargs.enable_thinking=false
            # 关掉思考链，strip_think 再兜一层。
            self.log("[llm] 该版本无 --reasoning-budget，改用请求体抑制思考链")
        if "-fa" not in supported and "--flash-attn" not in supported:
            self.log("[llm] 该版本无 flash-attn 开关，跳过")
        self.log("启动 " + " ".join(cmd[:4]) + " …")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 180
        while time.time() < deadline:
            if self._healthy(1.0):
                self.log("摘要模型就绪")
                return
            if self._proc.poll() is not None:
                raise RuntimeError(f"llama-server 退出（code {self._proc.returncode}）")
            time.sleep(0.3)
        raise RuntimeError("llama-server 启动超时")

    def warmup(self):
        try:
            self.generate("你是助手。", "回复：好", max_tokens=8, temperature=0.1)
        except Exception as e:
            self.log(f"预热失败: {e}")

    def close(self):
        p = self._proc
        self._proc = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()

    # ---------- 推理 ----------
    def generate(self, system: str, user: str, max_tokens: int = 1600,
                 temperature: float = 0.5, context_size: int = 8192,
                 stop: list[str] | None = None) -> str:
        with self._lock:
            self._ensure()
            body = {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
                "top_k": 20,
                "top_p": 0.8,
                "presence_penalty": 0.3,
                "repeat_penalty": 1.05,
                "stop": stop or ["<|im_end|>"],
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            req = urllib.request.Request(
                self._base() + "/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"推理失败 {e.code}: {e.read()[:300].decode('utf-8','replace')}")
            msg = data["choices"][0]["message"]
            text = strip_think(msg.get("content") or "")
            if not text and msg.get("reasoning_content"):
                text = strip_think(msg["reasoning_content"])
            n = data.get("usage", {}).get("completion_tokens", 0)
            self.log(f"[llm] {time.time() - t0:.1f}s, {len(text)} chars, {n} tokens")
            return text
