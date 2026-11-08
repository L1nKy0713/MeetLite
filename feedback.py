"""问题反馈：本地归档 + 可选上报 + 无后端兜底。

三条通道，按可用性依次回退：

1. **webhook**（可选）：config.json 里 `feedback_webhook` 填了地址时，POST 一份 JSON 过去。
   推荐部署 `server/feedback-worker.js`（Cloudflare Worker，免费额度过量级够用）——
   它在服务端用自己的 token 建 GitHub Issue，用户端不需要任何账号。
2. **本地归档**：无论如何都先写 `DATA_DIR/feedback/*.json`，断网也不会丢。
3. **GitHub 预填**：没配 webhook 或上报失败时，返回一个带标题/正文/诊断信息的
   `issues/new` 链接，用户用自己的 GitHub 账号一键提交。

诊断信息只包含版本、系统、模型文件名、日志尾部；
**不包含**会议标题、转写文本、笔记、录音、路径里的用户名（会被替换成 `~`）。
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import config

KINDS = ("bug", "idea", "other")
MAX_TITLE = 120
MAX_BODY = 4000
WEBHOOK_TIMEOUT = 8.0
GITHUB_REPO = "L1nKy0713/MeetLite"


def _dir() -> Path:
    d = config.DATA_DIR / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _redact(text: str) -> str:
    """把本机路径与用户名抹掉，避免反馈里带出隐私。"""
    if not text:
        return ""
    home = os.path.expanduser("~")
    if home:
        text = text.replace(home, "~")
    user = os.path.basename(home) if home else ""
    if user and len(user) >= 3:
        text = text.replace(user, "***")
    # 兜底：任何 /Users/xxx 或 C:\Users\xxx
    text = re.sub(r"(/Users/|/home/)[^/\s]+", r"\1***", text)
    text = re.sub(r"([A-Za-z]:\\Users\\)[^\\\s]+", r"\1***", text)
    return text


def _size_mb(p) -> str:
    try:
        return f"{Path(p).stat().st_size / 1048576:.0f} MB"
    except Exception:
        return "?"


def diagnostics(cfg, app=None) -> dict:
    """可安全外发的环境信息。"""
    models = {}
    for key, label in (("whisper_model", "asr"), ("summary_model", "llm")):
        p = cfg.get(key)
        models[label] = {"file": Path(p).name if p else None,
                         "size": _size_mb(p) if p else None}

    logs = []
    if app is not None and hasattr(app, "log_tail"):
        logs = [_redact(x) for x in app.log_tail(30)]

    d = {
        "version": config.APP_VERSION,
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "ui_lang": cfg.get("ui_lang") or "zh",
        "asr_lang": cfg.get("asr_lang") or "auto",
        "models": models,
        "threads": {"asr": cfg.get("whisper_threads"), "llm": cfg.get("llm_threads")},
        "log_tail": logs,
    }
    return d


def diag_text(diag: dict) -> str:
    """人类可读的诊断块，用于预览与 issue 正文。"""
    m = diag.get("models") or {}
    asr = m.get("asr") or {}
    llm = m.get("llm") or {}
    lines = [
        f"版本: MeetLite {diag.get('version')}",
        f"系统: {diag.get('os')} ({diag.get('machine')})",
        f"Python: {diag.get('python')}",
        f"界面/转写语言: {diag.get('ui_lang')} / {diag.get('asr_lang')}",
        f"转写模型: {asr.get('file') or '未设置'} ({asr.get('size') or '-'})",
        f"纪要模型: {llm.get('file') or '未设置'} ({llm.get('size') or '-'})",
    ]
    logs = diag.get("log_tail") or []
    if logs:
        lines.append("最近日志:")
        lines.extend("  " + x for x in logs[-30:])
    return "\n".join(lines)


def issue_url(title: str, body: str, diag: str) -> str:
    from urllib.parse import quote

    full = body
    if diag:
        full += "\n\n---\n<details><summary>环境信息</summary>\n\n```\n" + diag + "\n```\n\n</details>"
    full = full[:6000]
    return ("https://github.com/" + GITHUB_REPO + "/issues/new"
            + "?labels=feedback"
            + "&title=" + quote(title[:MAX_TITLE])
            + "&body=" + quote(full))


def _post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"MeetLite/{config.APP_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
            txt = r.read(400).decode("utf-8", "replace")
            ok = 200 <= r.status < 300
            return ok, txt
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def submit(cfg, app, data: dict) -> dict:
    """保存 + 尽力上报。返回 {ok, mode, id, file, url, error}。"""
    kind = (data.get("kind") or "bug").strip().lower()
    if kind not in KINDS:
        kind = "other"
    title = (data.get("title") or "").strip()[:MAX_TITLE]
    body = (data.get("body") or "").strip()[:MAX_BODY]
    contact = (data.get("contact") or "").strip()[:120]
    if not title:
        return {"ok": False, "error": "标题不能为空"}
    if len(body) < 5:
        return {"ok": False, "error": "描述太短，麻烦多写两句"}

    with_diag = bool(data.get("include_diag", True))
    diag = diagnostics(cfg, app) if with_diag else {}
    diag_s = diag_text(diag) if diag else ""

    rid = uuid.uuid4().hex[:10]
    payload = {
        "id": rid,
        "kind": kind,
        "title": title,
        "body": body,
        "contact": contact,
        "version": config.APP_VERSION,
        "os": platform.platform(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "diagnostics": diag,
    }

    # 1) 本地归档（永远先存）
    rec = dict(payload)
    rec["diag_text"] = diag_s
    fp = _dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{rid}.json"
    try:
        fp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        if app:
            app.log(f"[feedback] 本地归档失败 {e}")

    out = {"ok": False, "id": rid, "file": str(fp), "mode": "local"}

    # 2) webhook
    url = (cfg.get("feedback_webhook") or "").strip()
    if url:
        ok, msg = _post_webhook(url, payload)
        if ok:
            out.update({"ok": True, "mode": "webhook"})
            if app:
                app.log(f"[feedback] {rid} 已上报")
            return out
        out["error"] = f"上报失败：{msg}"
        if app:
            app.log(f"[feedback] {rid} 上报失败 {msg}")
    else:
        out["error"] = "未配置反馈接收地址"

    # 3) 兜底：GitHub 预填
    out["url"] = issue_url(f"[{kind}] {title}", body, diag_s)
    out["mode"] = "github"
    return out


def local_list(limit: int = 20) -> list[dict]:
    items = []
    for fp in sorted(_dir().glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            d.pop("diagnostics", None)
            d["file"] = fp.name
            items.append(d)
        except Exception:
            continue
    return items