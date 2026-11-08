"""模型管理：目录清单、多镜像下载、断点续传、校验、本地导入。

设计要点（对应「PCL 整合包 / 插件市场」的思路）：
  * 清单(models.json)是唯一事实来源：id / 类型 / 文件名 / 大小 / sha256 / 多镜像。
  * 下载走 Range 断点续传，先写 .part，校验通过才 rename 成正式文件。
  * 任何镜像都能用；本地压缩包 / 直链 URL 也走同一条校验安装路径。
  * 已安装 = 文件存在且大小匹配；选中项写进 config（asr_model / llm_model）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

import config as config_mod
import platforms

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "models.json")
CHUNK = 1 << 20

# 全局进度（单任务，前端轮询 /api/models/progress）
PROGRESS = {"running": False, "id": "", "done": 0, "total": 0,
            "phase": "", "error": "", "message": ""}


# ---------------------------------------------------------------- 清单
def _load_manifest() -> dict:
    try:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"models": []}


def catalog() -> list[dict]:
    """返回清单 + 是否已安装 + 落地路径。"""
    cfg = config_mod.load()
    out = []
    for m in _load_manifest().get("models", []):
        item = dict(m)
        path = installed_path(item)
        item["installed"] = bool(path)
        item["path"] = path or ""
        item["active"] = bool(path) and _is_active(cfg, item)
        out.append(item)
    return out


def find(model_id: str) -> dict | None:
    for m in _load_manifest().get("models", []):
        if m.get("id") == model_id:
            return dict(m)
    return None


def target_dir(kind: str) -> str:
    base = os.path.join(config_mod.DATA_DIR, "models")
    sub = "whisper" if kind == "whisper" else "summary"
    d = os.path.join(base, sub)
    os.makedirs(d, exist_ok=True)
    return d


def installed_path(item: dict) -> str:
    """清单里的相对文件名，先看本程序目录，再看本机其他工具留下的目录。"""
    name = os.path.basename(item.get("file") or "")
    if not name:
        return ""
    want = item.get("size") or 0
    kind = item.get("kind", "whisper")
    sub = "whisper" if kind == "whisper" else "summary"
    candidates = [os.path.join(target_dir(kind), name)]
    for d in platforms.legacy_dirs():
        candidates.append(os.path.join(str(d), "models", sub, name))
        candidates.append(os.path.join(str(d), "models", name))
    for p in candidates:
        try:
            if os.path.isfile(p) and (not want or abs(os.path.getsize(p) - want) <= 4096):
                return p
        except OSError:
            continue
    return ""


def _is_active(cfg, item: dict) -> bool:
    key = "whisper_model" if item.get("kind") == "whisper" else "summary_model"
    cur = os.path.basename(str(cfg.get(key) or ""))
    return bool(cur) and cur == os.path.basename(item.get("file") or "")


# ---------------------------------------------------------------- 下载
def _sha256(path: str, progress=None) -> str:
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(8 * CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total, "校验中")
    return h.hexdigest()


def _fetch(url: str, dest: str, progress=None) -> None:
    """支持 Range 断点续传；.part 用于中断恢复。"""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "MeetLite/1.0"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0) + have
        mode = "ab" if (have and r.status == 206) else "wb"
        if mode == "wb":
            have = 0
        with open(part, mode) as f:
            done = have
            while True:
                b = r.read(CHUNK)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress:
                    progress(done, total or done, "下载中")
    os.replace(part, dest)


def install(model_id: str, progress=None, url: str = "") -> dict:
    """下载并安装一个清单模型。返回 {ok, path} 或抛异常。"""
    item = find(model_id)
    if not item:
        raise RuntimeError(f"未知模型：{model_id}")
    name = os.path.basename(item["file"])
    dest = os.path.join(target_dir(item["kind"]), name)
    if os.path.isfile(dest) and (not item.get("size")
                                 or abs(os.path.getsize(dest) - item["size"]) <= 4096):
        return {"ok": True, "path": dest, "skipped": True}

    sources = [url] if url else []
    sources += [s for s in (item.get("sources") or []) if s]
    if not sources:
        raise RuntimeError("该模型没有内置下载源：请在上方粘贴直链，或点「导入文件」选择本地模型文件")

    last = None
    for src in sources:
        try:
            if progress:
                progress(0, item.get("size") or 0, "连接 " + _short(src))
            _fetch(src, dest + ".tmp", progress)
            if item.get("sha256"):
                got = _sha256(dest + ".tmp", progress)
                if got.lower() != str(item["sha256"]).lower():
                    raise RuntimeError(f"sha256 不符（{got[:12]}…）")
            os.replace(dest + ".tmp", dest)
            return {"ok": True, "path": dest, "source": src}
        except Exception as e:  # 换下一个镜像
            last = e
            for junk in (dest + ".tmp", dest + ".tmp.part"):
                try:
                    os.remove(junk)
                except OSError:
                    pass
            continue
    raise RuntimeError(f"下载失败：{last}")


def _short(url: str) -> str:
    return url.split("//", 1)[-1][:48]


# ---------------------------------------------------------------- 本地导入
ARCHIVE_EXT = (".zip", ".tar", ".gz", ".7z", ".xz", ".bz2")


def import_local(path: str, kind: str = "", progress=None) -> dict:
    """从本地文件或压缩包导入模型。

    - 直接把 .bin/.gguf 拷进程序模型目录；
    - zip 里找出所有 .bin/.gguf，挑最大的那个；
    - 有 sha256 的清单项一并校验。
    """
    if not os.path.isfile(path):
        raise RuntimeError("文件不存在")
    name = os.path.basename(path)
    lower = name.lower()

    if lower.endswith(ARCHIVE_EXT):
        tmpd = tempfile.mkdtemp(prefix="meetlite-model-")
        try:
            if lower.endswith(".zip"):
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmpd)
            else:
                raise RuntimeError("目前只支持 zip 压缩包（tar/7z 请先解开）")
            best, best_sz = "", 0
            for root, _dirs, files in os.walk(tmpd):
                for f in files:
                    if f.lower().endswith((".bin", ".gguf")):
                        p = os.path.join(root, f)
                        sz = os.path.getsize(p)
                        if sz > best_sz:
                            best, best_sz = p, sz
            if not best:
                raise RuntimeError("压缩包里没有 .bin / .gguf 模型文件")
            path = best
            name = os.path.basename(best)
        except zipfile.BadZipFile:
            shutil.rmtree(tmpd, ignore_errors=True)
            raise RuntimeError("压缩包损坏")
        keep_tmp = tmpd
    else:
        keep_tmp = ""

    if not name.lower().endswith((".bin", ".gguf")):
        raise RuntimeError("只认 .bin（whisper.cpp）或 .gguf（llama.cpp）")

    if not kind:
        kind = "whisper" if name.lower().endswith(".bin") else "whisper"
        if name.lower().endswith(".gguf"):
            kind = "summary"

    dest = os.path.join(target_dir(kind), name)
    if progress:
        progress(0, os.path.getsize(path), "复制中")
    shutil.copy2(path, dest + ".tmp")
    if progress:
        progress(os.path.getsize(dest + ".tmp"), os.path.getsize(dest + ".tmp"), "复制中")
    os.replace(dest + ".tmp", dest)
    if keep_tmp:
        shutil.rmtree(keep_tmp, ignore_errors=True)

    return {"ok": True, "path": dest, "kind": kind, "name": name,
            "size": os.path.getsize(dest)}


def verify(path: str, sha256: str = "") -> dict:
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    got = _sha256(path)
    ok = (not sha256) or got.lower() == sha256.lower()
    return {"ok": ok, "sha256": got, "size": os.path.getsize(path)}