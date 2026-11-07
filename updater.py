"""MeetLite 自更新：清单检查 → 下载校验 → 换包重启。

清单（update.json）例子：
{
  "version": "1.0.1",
  "notes": "修了几处 VAD 判定；新增模型面板",
  "published": "2026-11-08",
  "macos":   {"url": "https://…/MeetLite-1.0.1.zip", "size": 2210000, "sha256": "…"},
  "windows": {"url": "https://…/MeetLite-1.0.1-win.zip", "size": 4200000, "sha256": "…"}
}

设计取舍：
- 不做增量（bsdiff 之类）：整包才 2MB，模型不进包，不值得引入 diff 复杂度。
- 不做静默自动装：换包要重启，必须让用户点一下，否则正在录音会被打断。
- 换包用「外部脚本等父进程退出再 mv」：运行中的 .app 不能就地覆盖，脚本脱离进程组后
  等 server 与 launcher 都退出，再做 rename 交换，失败可回滚到 .old。
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shutil
import subprocess
import sys
import tempfile
import concurrent.futures
import threading
import urllib.error
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import config as config_mod
import platforms

DATA_DIR = config_mod.DATA_DIR
UPDATE_DIR = DATA_DIR / "updates"
UA = "MeetLite-Updater/1.0"

PROGRESS = {
    "running": False,
    "phase": "",       # check | download | verify | unpack | apply | done | error
    "done": 0,
    "total": 0,
    "error": "",
    "message": "",
    "version": "",
}
_LOCK = threading.Lock()


# ---------- 版本 ----------

def current() -> str:
    return config_mod.APP_VERSION


def _tuple(v: str) -> tuple:
    out = []
    for part in str(v or "").strip().lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def is_newer(remote: str, local: str | None = None) -> bool:
    return _tuple(remote) > _tuple(local or current())


# ---------- 平台 / 运行位置 ----------

def platform_key() -> str:
    return "windows" if platforms.IS_WIN else "macos"


def app_bundle() -> Path | None:
    """打包后 server.py 在 <X>.app/Contents/Resources/app/ 里，反推 .app 路径。"""
    here = Path(__file__).resolve().parent
    if here.parent.name == "Resources" and here.parent.parent.name == "Contents":
        return here.parent.parent.parent
    if platforms.IS_WIN:
        # Windows 打包结构：<dir>/app/*.py，<dir>/MeetLite.exe
        exe = here.parent / "MeetLite.exe"
        if exe.exists():
            return here.parent
        exe2 = here / "MeetLite.exe"
        if exe2.exists():
            return here
    return None


def can_self_update() -> bool:
    return app_bundle() is not None


# ---------- 检查 ----------

def _get_one(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(2 * 1024 * 1024)
    return json.loads(raw.decode("utf-8"))


def mirrors(url: str) -> list[str]:
    """同一份清单的多个可达地址。国内直连 GitHub raw 常被拖到超时，因此同时
    给出 jsDelivr 各 CDN 与 raw 变体；check() 会并行取，选版本号最大的那份，
    这样即使 CDN 缓存了旧清单也不会漏掉新版本。"""
    out = [url]
    gh = re.match(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", url)
    jsd = re.match(r"^https://[a-z0-9.]*jsdelivr\.net/gh/([^/]+)/([^/@]+)@([^/]+)/(.+)$", url)
    m = gh or jsd
    if m:
        user, repo, branch, path = m.groups()
        hosts = ["https://fastly.jsdelivr.net", "https://cdn.jsdelivr.net",
                 "https://gcore.jsdelivr.net", "https://testingcf.jsdelivr.net"]
        out += [f"{h}/gh/{user}/{repo}@{branch}/{path}" for h in hosts]
        out += [f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"]
    return list(dict.fromkeys(out))


def _get_manifest(url: str, timeout: float = 10.0) -> dict:
    """并行拉取所有镜像，返回版本号最高的清单（避免 CDN 旧缓存遮蔽新版）。"""
    cands = mirrors(url)
    best: dict | None = None
    errs: list[str] = []
    ex = ThreadPoolExecutor(max_workers=max(1, min(6, len(cands))))
    try:
        futs = {ex.submit(_get_one, u, timeout): u for u in cands}
        try:
            for f in as_completed(futs, timeout=timeout + 4):
                try:
                    m = f.result()
                except Exception as e:  # noqa: BLE001 - 单源失败不影响其它
                    errs.append(f"{futs[f].split('/')[2]}: {type(e).__name__}")
                    continue
                v = str(m.get("version") or "").strip()
                if not v:
                    continue
                if best is None or _tuple(v) > _tuple(str(best.get("version") or "0")):
                    best = m
        except TimeoutError:
            errs.append("并行拉取超时")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if best is not None:
        return best
    raise RuntimeError(" / ".join(errs[-4:]) or "所有镜像均不可用")


def check(url: str | None = None) -> dict:
    cfg = config_mod.load()
    src = (url or cfg.get("update_url") or "").strip()
    if not src:
        return {"ok": False, "error": "未配置更新地址", "current": current()}
    with _LOCK:
        PROGRESS.update(running=True, phase="check", done=0, total=0, error="", message="", version="")
    try:
        man = _get_manifest(src)
    except urllib.error.HTTPError as e:
        return _fail(f"更新清单返回 {e.code}")
    except Exception as e:  # 网络超时 / DNS / JSON
        return _fail(f"读取更新清单失败：{e}")
    remote = str(man.get("version") or "").strip()
    if not remote:
        return _fail("更新清单缺少 version 字段")
    asset = man.get(platform_key()) or {}
    has = is_newer(remote)
    with _LOCK:
        PROGRESS.update(running=False, phase="done" if has else "idle", message="")
    return {
        "ok": True,
        "current": current(),
        "remote": remote,
        "has_update": has,
        "notes": man.get("notes") or "",
        "published": man.get("published") or "",
        "size": int(asset.get("size") or 0),
        "can_self_update": can_self_update(),
        "platform": platform_key(),
    }


def _fail(msg: str) -> dict:
    with _LOCK:
        PROGRESS.update(running=False, phase="error", error=msg, message="")
    return {"ok": False, "error": msg, "current": current()}


# ---------- 下载 ----------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


RELEASE_PROXIES = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://hk.gh-proxy.com/",
]


def asset_mirrors(url: str) -> list[str]:
    """GitHub Release 资产在国内经常直连超时，派生一组加速代理做降级。"""
    out = [url]
    m = re.match(r"^https://github\.com/([^/]+/[^/]+/releases/download/.+)$", url)
    if m:
        out += [p + "https://github.com/" + m.group(1) for p in RELEASE_PROXIES]
    return list(dict.fromkeys(out))


def _pull(url: str, part: Path) -> None:
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        resp = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return
        raise
    total = int(resp.headers.get("Content-Length") or 0) + (have if resp.status == 206 else 0)
    mode = "ab" if (have and resp.status == 206) else "wb"
    if mode == "wb":
        have = 0
    with _LOCK:
        PROGRESS.update(phase="download", done=have, total=total)
    with open(part, mode) as f:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            have += len(chunk)
            with _LOCK:
                PROGRESS["done"] = have


def _probe(url: str, timeout: float = 8.0, nbytes: int = 262144) -> float:
    """抓 256KB 给镜像测速，返回「传完整个包预计耗时」；失败返回 inf。"""
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes=0-{nbytes - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = len(r.read(nbytes))
        dt = max(time.time() - t0, 0.05)
        if not got:
            return float("inf")
        size = int(PROGRESS.get("total") or 0) or 2400 * 1024
        return size / (got / dt)  # 预计秒数
    except Exception:  # noqa: BLE001 - 探测失败即淘汰
        return float("inf")


def _download(url: str, dest: Path, expect: int = 0) -> Path:
    part = dest.with_suffix(dest.suffix + ".part")
    cands = asset_mirrors(url)
    with _LOCK:
        PROGRESS.update(phase="download", message="选择下载线路")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cands)) as ex:
        times = list(ex.map(_probe, cands))
    scored = sorted(zip(times, cands))
    with _LOCK:
        PROGRESS["lines"] = [f"{u.split('/')[2]} {p:.1f}s" if p != float("inf") else f"{u.split('/')[2]} ✗"
                             for p, u in scored]
    errs = []
    for _, u in scored:
        if _ == float("inf"):
            continue
        try:
            _pull(u, part)
            got = part.stat().st_size if part.exists() else 0
            if got and (not expect or got == expect):
                os.replace(part, dest)
                with _LOCK:
                    PROGRESS["line"] = u.split("/")[2]
                return dest
            errs.append(f"{u.split('/')[2]}: {got}B")
        except Exception as e:  # noqa: BLE001 - 逐线路降级
            errs.append(f"{u.split('/')[2]}: {type(e).__name__}")
            continue
    raise RuntimeError("下载失败：" + (" / ".join(errs[-3:]) or "所有线路不可达"))


# ---------- 解包 / 换包 ----------

def _unpack(zip_path: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    if platforms.IS_WIN:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(work)
    else:
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(work)], check=True)
    # 找包里的 .app / exe 根
    if platforms.IS_WIN:
        for cand in sorted(work.iterdir()):
            if cand.is_dir() and (cand / "MeetLite.exe").exists():
                return cand
        if (work / "MeetLite.exe").exists():
            return work
        raise RuntimeError("更新包里找不到 MeetLite.exe")
    apps = [p for p in sorted(work.iterdir()) if p.suffix == ".app"]
    if not apps:
        raise RuntimeError("更新包里找不到 .app")
    return apps[0]


def _write_mac_script(staged: Path, target: Path, log: Path) -> Path:
    script = UPDATE_DIR / "apply.sh"
    script.write_text(
        f"""#!/bin/bash
exec >>"{log}" 2>&1
echo "[apply] start $(date)"
for i in $(seq 1 120); do
  kill -0 {os.getpid()} 2>/dev/null || break
  sleep 0.5
done
# 干掉仍在跑的外壳，否则 open 只会激活旧实例、不会加载新包
pkill -f "{target}/Contents/MacOS/" 2>/dev/null
pkill -f "{target}/Contents/Resources/app/server.py" 2>/dev/null
for i in $(seq 1 60); do
  pgrep -f "{target}/Contents/MacOS/" >/dev/null || break
  sleep 0.5
done
sleep 0.4
OLD="{target}.old"
rm -rf "$OLD"
if mv "{target}" "$OLD"; then
  if mv "{staged}" "{target}"; then
    xattr -dr com.apple.quarantine "{target}" 2>/dev/null || true
    echo "[apply] swapped, relaunch"
    open "{target}"
  else
    echo "[apply] move new failed, rollback"
    mv "$OLD" "{target}"
  fi
else
  echo "[apply] move old failed"
fi
sleep 1
rm -rf "$OLD"
rm -rf "{staged.parent}"
echo "[apply] done $(date)"
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _write_win_script(staged: Path, target: Path, log: Path) -> Path:
    script = UPDATE_DIR / "apply.cmd"
    script.write_text(
        "@echo off\r\n"
        f"echo [apply] start %DATE% %TIME% >> \"{log}\"\r\n"
        ":wait\r\n"
        f"tasklist /FI \"PID eq {os.getpid()}\" | find \"{os.getpid()}\" >nul && (timeout /t 1 >nul & goto wait)\r\n"
        f"timeout /t 2 >nul\r\n"
        f"move /Y \"{target}\" \"{target}.old\" >> \"{log}\" 2>&1\r\n"
        f"xcopy /E /I /Y \"{staged}\\*\" \"{target}\\\" >> \"{log}\" 2>&1\r\n"
        f"start \"\" \"{target}\\MeetLite.exe\"\r\n"
        f"timeout /t 3 >nul\r\n"
        f"rmdir /S /Q \"{target}.old\" >> \"{log}\" 2>&1\r\n"
        f"rmdir /S /Q \"{staged.parent}\" >> \"{log}\" 2>&1\r\n",
        encoding="utf-8",
    )
    return script


def download_and_stage(version_hint: str = "", url: str | None = None) -> dict:
    """下载 + 校验 + 解包，返回待安装路径；不换包。"""
    cfg = config_mod.load()
    src = (url or cfg.get("update_url") or "").strip()
    try:
        man = _get_manifest(src)
        remote = str(man.get("version") or version_hint or "").strip()
        asset = man.get(platform_key()) or {}
        link = (asset.get("url") or "").strip()
        want_sha = (asset.get("sha256") or "").strip().lower()
        if not link:
            return _fail("清单里没有当前平台的下载地址")
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        name = os.path.basename(link.split("?")[0]) or f"MeetLite-{remote}.zip"
        zip_path = UPDATE_DIR / name
        with _LOCK:
            PROGRESS.update(running=True, phase="download", done=0, total=int(asset.get("size") or 0),
                            error="", message=remote, version=remote)
        _download(link, zip_path, expect=int(asset.get("size") or 0))
        with _LOCK:
            PROGRESS.update(phase="verify", message=remote)
        got = _sha256(zip_path)
        if want_sha and got != want_sha:
            zip_path.unlink(missing_ok=True)
            return _fail("更新包校验失败（sha256 不匹配），已删除")
        with _LOCK:
            PROGRESS.update(phase="unpack", message=remote)
        work = Path(tempfile.mkdtemp(prefix="update_", dir=str(UPDATE_DIR)))
        staged = _unpack(zip_path, work)
        with _LOCK:
            PROGRESS.update(running=False, phase="ready", message=remote)
        return {"ok": True, "version": remote, "staged": str(staged), "zip": str(zip_path),
                "sha256": got, "notes": man.get("notes") or ""}
    except Exception as e:
        return _fail(f"下载更新失败：{e}")


def apply_staged(staged: str) -> dict:
    """换包：写脚本 → 脱离进程组启动 → 调用方随后自行退出。"""
    target = app_bundle()
    if not target:
        return {"ok": False, "error": "开发模式下不执行自更新（请手动替换）"}
    staged_path = Path(staged)
    if not staged_path.exists():
        return {"ok": False, "error": "更新包已不存在，请重新下载"}
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    log = UPDATE_DIR / "apply.log"
    if platforms.IS_WIN:
        script = _write_win_script(staged_path, target, log)
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=0x00000008, close_fds=True)
    else:
        script = _write_mac_script(staged_path, target, log)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    with _LOCK:
        PROGRESS.update(running=False, phase="apply")
    return {"ok": True, "log": str(log), "target": str(target)}


def cleanup() -> None:
    """启动时清掉上次的残留：老的 .old、应用脚本、解包目录。"""
    try:
        if not UPDATE_DIR.exists():
            return
        for p in UPDATE_DIR.glob("update_*"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        for name in ("apply.sh", "apply.cmd"):
            f = UPDATE_DIR / name
            if f.exists():
                f.unlink()
        target = app_bundle()
        if target:
            old = Path(str(target) + ".old")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass