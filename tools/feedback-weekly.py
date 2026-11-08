#!/usr/bin/env python3
"""每周拉一次用户反馈，生成 Markdown 周报。

用法：
    python3 tools/feedback-weekly.py                # 拉最近 7 天
    python3 tools/feedback-weekly.py --days 30
    python3 tools/feedback-weekly.py --since 2026-11-01

token 读取顺序：环境变量 GH_TOKEN → ~/.config/meetlite/gh_token → git credential（不尝试）。
输出：feedback/digest/<北京时间日期>.md，同时在终端打印摘要。

只用标准库；macOS / Windows / Linux 都能跑（Windows 用任务计划程序，见 README）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = os.environ.get("MEETLITE_REPO", "L1nKy0713/MeetLite")
API = "https://api.github.com"
KIND_TITLE = {"bug": "🐞 问题", "idea": "💡 建议", "other": "📝 其他"}
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "feedback" / "digest"


def token() -> str:
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    p = Path.home() / ".config" / "meetlite" / "gh_token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    sys.exit("缺少 token：设置 GH_TOKEN，或把 PAT 写进 ~/.config/meetlite/gh_token")


def api_get(path: str, tok: str) -> list:
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": "Bearer " + tok,
            "Accept": "application/vnd.github+json",
            "User-Agent": "meetlite-weekly",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as f:
        return json.load(f)


def fetch_issues(tok: str, since: str) -> list:
    out: list = []
    page = 1
    while True:
        batch = api_get(
            f"/repos/{REPO}/issues?state=all&labels=feedback&per_page=100&page={page}&since={since}",
            tok,
        )
        if not batch:
            return out
        out.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            return out
        page += 1


def kind_of(issue: dict) -> str:
    title = issue.get("title") or ""
    if title.startswith("[bug]"):
        return "bug"
    if title.startswith("[idea]"):
        return "idea"
    return "other"


def clean_title(issue: dict) -> str:
    t = issue.get("title") or ""
    for k in ("[bug]", "[idea]", "[other]"):
        if t.startswith(k):
            return t[len(k) :].strip()
    return t.strip()


def esc(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def render(issues: list, since: datetime, now: datetime) -> str:
    lines = [f"# 反馈周报 · {now:%Y-%m-%d}", ""]
    lines.append(
        f"共 **{len(issues)}** 条（label=feedback，{since:%Y-%m-%d} 之后有更新）。"
    )
    lines.append("")
    if not issues:
        lines.append("这周没有新反馈。")
        return "\n".join(lines) + "\n"
    lines += ["| 类型 | 标题 | 状态 | 创建 | 链接 |", "| --- | --- | --- | --- | --- |"]
    for i in issues:
        lines.append(
            f"| {KIND_TITLE[kind_of(i)]} | {esc(clean_title(i))} | {i.get('state')} "
            f"| {(i.get('created_at') or '')[:10]} | [#{i['number']}]({i.get('html_url')}) |"
        )
    lines += ["", "## 详情"]
    for i in issues:
        body = (i.get("body") or "").strip()
        lines += ["", f"### #{i['number']} {esc(clean_title(i))}", ""]
        lines.append(
            f"- 类型：{KIND_TITLE[kind_of(i)]}　状态：{i.get('state')}　创建：{(i.get('created_at') or '')[:10]}"
        )
        lines.append(f"- 链接：{i.get('html_url')}")
        if body:
            lines += ["", body if len(body) <= 1500 else body[:1500] + "\n\n…（截断）"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", help="ISO 时间，优先于 --days")
    ap.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    ap.add_argument("--no-push", action="store_true", help="不推微信")
    ap.add_argument("--push-only", action="store_true", help="不拉取，只把最新日报再推一次")
    args = ap.parse_args()

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=tz)
        if args.since
        else now - timedelta(days=args.days)
    )
    if args.push_only:
        files = sorted(OUT_DIR.glob("*.md"))
        if not files:
            sys.exit(f"{OUT_DIR} 里还没有日报，先跑一次不带 --push-only 的")
        latest = files[-1]
        print(f"→ 只推送：{latest}")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notify
        res = notify.push(latest.read_text(encoding="utf-8"), latest, f"反馈周报 · {latest.stem}", None)
        for r in res or []:
            print(("✓ " if r.get("ok") else "✗ ") + str(r.get("channel")) + " " + str(r.get("sent") or r.get("error") or ""))
        return 0 if not res or any(r.get("ok") for r in res) else 1

    issues = fetch_issues(token(), since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    issues.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    md = render(issues, since, now)

    if args.stdout:
        sys.stdout.write(md)
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{now:%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    print(f"✓ {path}（{len(issues)} 条）")

    if not args.no_push:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import notify  # noqa: WPS433
            results = notify.push(md, path, f"反馈周报 · {now:%Y-%m-%d}", len(issues))
        except Exception as e:
            print(f"[push] 跳过：{type(e).__name__}: {e}", file=sys.stderr)
            return 0
        if not results:
            print("[push] 没配置推送通道，跳过（见 tools/notify.py 顶部说明）")
        for r in results:
            flag = "✓" if r.get("ok") else "✗"
            print(f"[push] {flag} {r.get('channel')} {r.get('sent') or ''} {r.get('error') or ''}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())