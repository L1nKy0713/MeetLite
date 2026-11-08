#!/usr/bin/env python3
"""把反馈周报推到微信（企业微信群机器人 / Server酱 / WxPusher）。

设计成可选模块：没有配置文件时什么也不做，不报错。

配置文件（任选其一，先找到的先用）：
    ~/.config/meetlite/notify.json
    ~/Library/Application Support/MeetLite/notify.json      (macOS)
    %APPDATA%\\MeetLite\\notify.json                        (Windows)

内容示例（只填你要用的那条，其余留空或删掉）：

    {
      "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx",
      "wecom_file": true,            // 除文字卡片外，再把 .md 当文件发过去
      "serverchan": "SCT xxxxxxxx",  // 或只填 SendKey
      "wxpusher": { "app_token": "AT_xxx", "uids": ["UID_xxx"] }
    }

命令行自测：
    python3 tools/notify.py --test
    python3 tools/notify.py --file feedback/digest/2026-11-08.md --card "反馈周报 · 2026-11-08|共 3 条"
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TIMEOUT = 20.0
WECOM_MD_LIMIT = 3800  # 官方上限 4096 字节，留点余量


# ---------------------------------------------------------------- 配置

def _candidates() -> list[Path]:
    out = [Path.home() / ".config" / "meetlite" / "notify.json"]
    if sys.platform == "darwin":
        out.append(Path.home() / "Library/Application Support/MeetLite/notify.json")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        out.append(Path(base) / "MeetLite/notify.json")
    else:
        out.append(Path.home() / ".local/share/MeetLite/notify.json")
    return out


def config_path() -> Path | None:
    env = os.environ.get("MEETLITE_NOTIFY_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    for p in _candidates():
        if p.exists():
            return p
    return None


def load_config() -> dict:
    """配置文件 + 环境变量叠加。环境变量优先，方便 CI 用 secrets。"""
    env_cfg = {}
    if os.environ.get("WECOM_WEBHOOK"):
        env_cfg["wecom_webhook"] = os.environ["WECOM_WEBHOOK"]
    if os.environ.get("WECOM_FILE"):
        env_cfg["wecom_file"] = os.environ["WECOM_FILE"].lower() not in ("0", "false", "no")
    if os.environ.get("SCT_KEY"):
        env_cfg["serverchan"] = os.environ["SCT_KEY"]
    if os.environ.get("WXPUSHER_TOKEN"):
        env_cfg["wxpusher"] = {
            "app_token": os.environ["WXPUSHER_TOKEN"],
            "uids": [u for u in (os.environ.get("WXPUSHER_UIDS") or "").split(",") if u],
        }

    p = config_path()
    if not p:
        return env_cfg
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # 配置写坏了不应该让周报任务崩
        print(f"[notify] 配置读不出来：{p} → {e}", file=sys.stderr)
        return env_cfg
    cfg.update(env_cfg) if isinstance(cfg, dict) else None
    return cfg if isinstance(cfg, dict) else env_cfg


# ---------------------------------------------------------------- 基础 HTTP

def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
        body = f.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"errcode": -1, "errmsg": body[:300]}


def _post_multipart(url: str, field: str, filename: str, content: bytes) -> dict:
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary = "----meetlite" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
        raw = f.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"errcode": -1, "errmsg": raw[:300]}


# ---------------------------------------------------------------- 文本处理

def truncate_bytes(text: str, limit: int = WECOM_MD_LIMIT) -> str:
    """按 UTF-8 字节截断，别把多字节字符劈开。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[: limit - 40].decode("utf-8", "ignore")
    return cut + "\n\n…（内容较长，完整版见随附的 .md 文件）"


def digest_card(md_text: str, title: str, count: int | None = None) -> str:
    """把带表格的 Markdown 折成企业微信能渲染的卡片（它不支持表格）。"""
    lines = [f"### {title}"]
    if count is not None:
        lines.append(f'<font color="comment">共 {count} 条，完整内容见文件</font>')
    rows: list[str] = []
    in_detail = False
    for ln in md_text.splitlines():
        s = ln.strip()
        if s.startswith("|") and not set(s) <= set("|-: "):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 5 and cells[0] not in ("类型",):
                kind, subj, state, created, link = cells[:5]
                rows.append(f"- {kind} **{subj}** · {state} · {created}\n  {link}")
        elif s.startswith("## 详情"):
            in_detail = True
        elif s.startswith("这周没有新反馈"):
            rows.append("这周没有新反馈。")
    if not rows:
        rows = [ln for ln in md_text.splitlines() if ln.strip().startswith("- ")][:20] or [md_text[:400]]
    lines += ["", *rows[:25]]
    if in_detail and len(rows) > 25:
        lines.append(f"\n<font color=\"comment\">其余 {len(rows) - 25} 条见文件</font>")
    return truncate_bytes("\n".join(lines))


# ---------------------------------------------------------------- 通道

def push_wecom(cfg: dict, card: str, path: Path | None) -> dict:
    hook = (cfg.get("wecom_webhook") or "").strip()
    if not hook:
        return {"ok": False, "channel": "wecom", "error": "未配置 wecom_webhook"}
    try:
        r1 = _post_json(hook, {"msgtype": "markdown", "markdown": {"content": card}})
    except Exception as e:
        return {"ok": False, "channel": "wecom", "error": f"{type(e).__name__}: {e}"}
    if r1.get("errcode") != 0:
        return {"ok": False, "channel": "wecom", "error": r1.get("errmsg") or str(r1)}

    res = {"ok": True, "channel": "wecom", "sent": "markdown"}
    if not (cfg.get("wecom_file") and path and path.exists()):
        return res

    key = hook.split("key=", 1)[-1].split("&", 1)[0]
    upload = hook.split("/send", 1)[0] + "/upload_media?key=" + key + "&type=file"
    try:
        up = _post_multipart(upload, "media", path.name, path.read_bytes())
        if up.get("errcode") != 0 or not up.get("media_id"):
            res["file_error"] = up.get("errmsg") or "上传失败"
            return res
        r2 = _post_json(hook, {"msgtype": "file", "file": {"media_id": up["media_id"]}})
        if r2.get("errcode") == 0:
            res["sent"] += " +file"
        else:
            res["file_error"] = r2.get("errmsg") or str(r2)
    except Exception as e:
        res["file_error"] = f"{type(e).__name__}: {e}"
    return res


def push_serverchan(cfg: dict, card: str, path: Path | None) -> dict:
    key = (cfg.get("serverchan") or "").strip().replace("SCT", "").strip()
    if not key:
        return {"ok": False, "channel": "serverchan", "error": "未配置 serverchan"}
    url = f"https://sctapi.ftqq.com/{key}.send"
    body = {"title": "MeetLite 反馈周报", "desp": card}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            r = json.loads(f.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "channel": "serverchan", "error": f"{type(e).__name__}: {e}"}
    ok = r.get("code") in (0, "0")
    return {"ok": ok, "channel": "serverchan", "sent": "md", "error": None if ok else r.get("message")}


def push_wxpusher(cfg: dict, card: str) -> dict:
    w = cfg.get("wxpusher") or {}
    token = (w.get("app_token") or "").strip()
    if not token:
        return {"ok": False, "channel": "wxpusher", "error": "未配置 wxpusher"}
    payload = {
        "appToken": token,
        "content": card,
        "summary": "MeetLite 反馈周报",
        "contentType": 3,  # 3 = markdown
        "uids": w.get("uids") or [],
    }
    try:
        r = _post_json("https://wxpusher.zjiecode.com/api/send/message", payload)
    except Exception as e:
        return {"ok": False, "channel": "wxpusher", "error": f"{type(e).__name__}: {e}"}
    ok = r.get("code") == 1000
    return {"ok": ok, "channel": "wxpusher", "sent": "md", "error": None if ok else r.get("msg")}


def push(md_text: str, path: Path | None = None, title: str = "MeetLite 反馈周报",
         count: int | None = None, cfg: dict | None = None) -> list[dict]:
    """按配置逐个通道推；返回每个通道的结果。没有任何通道时返回空列表。"""
    cfg = load_config() if cfg is None else cfg
    if not cfg:
        return []
    card = digest_card(md_text, title, count)
    results = []
    if cfg.get("wecom_webhook"):
        results.append(push_wecom(cfg, card, path))
    if cfg.get("serverchan"):
        results.append(push_serverchan(cfg, card, path))
    if cfg.get("wxpusher"):
        results.append(push_wxpusher(cfg, card))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="要推送的 .md 路径")
    ap.add_argument("--card", help="自定义卡片文案，格式『标题|副标题』")
    ap.add_argument("--test", action="store_true", help="推一条测试消息")
    args = ap.parse_args()

    p = config_path()
    print(f"配置：{p or '没找到（先创建 ~/.config/meetlite/notify.json）'}")

    if args.test:
        md = "# 测试\n\n| 类型 | 标题 | 状态 | 创建 | 链接 |\n| --- | --- | --- | --- | --- |\n| 🐞 | 这是一条测试反馈 | open | 2026-11-08 | [#1](https://example.com) |"
        res = push(md, None, "MeetLite 反馈周报（测试）", 1)
    elif args.file:
        path = Path(args.file)
        text = path.read_text(encoding="utf-8")
        title = "MeetLite 反馈周报"
        if args.card and "|" in args.card:
            title = args.card.split("|", 1)[0]
        res = push(text, path, title, None)
    else:
        ap.print_help()
        return 2

    if not res:
        print("没有可用的通道（配置里 wecom_webhook / serverchan / wxpusher 都空）。")
        return 1
    ok = False
    for r in res:
        flag = "✓" if r.get("ok") else "✗"
        print(f"{flag} {r.get('channel')} {r.get('sent') or ''} {r.get('error') or ''}".rstrip())
        ok = ok or bool(r.get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())