"""回收站：删除的会议先落到磁盘目录，保留 N 天后自动清理。

设计要点
--------
* 每条会议的全部数据（meeting / segments / summary / notes）导成一个
  JSON 快照，文件名 `<毫秒时间戳>-<会议id>.json`，放在
  `DATA_DIR/trash/` 下。文件很小（一条会议通常 2–20 KB），所以"移动"
  的代价几乎为零，也不需要搬运音频（本程序不保存音频）。
* 恢复时按原 id 写回；若同一个 id 已存在，则分配新 id 并提示。
* 过期清理走三层保险：服务启动时清一次、后台线程每小时清一次、
  每次列回收站时再清一次（惰性清理）。这样即使 App 长时间不开，
  下次打开也会立刻把过期项去掉。
* 目录本身就是可见的备份：想手动留档，直接把 json 拷走即可。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_DAYS = 7
SUFFIX = ".json"


def trash_dir() -> Path:
    from config import DATA_DIR  # 延迟导入，避免循环依赖

    d = Path(DATA_DIR) / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fname(mid: str, ts: float | None = None) -> str:
    ts = ts if ts is not None else time.time()
    return f"{int(ts * 1000)}-{mid}{SUFFIX}"


def _safe_name(name: str) -> str:
    """只允许回收站文件名，挡住 ../ 之类的路径穿越。"""
    name = os.path.basename(str(name or "").strip())
    if not name.endswith(SUFFIX) or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid trash entry")
    return name


# ---------------------------------------------------------------- 写入
def move_to_trash(store, mid: str) -> dict:
    """把会议导出成快照，再从数据库彻底删除。返回 {ok, entry, ...}。"""
    snap = store.snapshot(mid)
    if not snap or not snap.get("meeting"):
        return {"ok": False, "error": "会议不存在"}

    ts = time.time()
    payload = {
        "version": 1,
        "id": mid,
        "title": snap["meeting"].get("title") or "(无标题)",
        "deleted_at": ts,
        "segments_count": len(snap.get("segments") or []),
        "has_summary": bool((snap.get("summary") or {}).get("content")),
        "has_notes": bool(snap.get("notes")),
        "data": snap,
    }
    name = _fname(mid, ts)
    path = trash_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # 原子落盘，避免半截文件

    store.delete_meeting(mid)  # 硬删除
    return {"ok": True, "entry": name, "title": payload["title"], "deleted_at": ts}


# ---------------------------------------------------------------- 读取
def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_items(days: int = DEFAULT_DAYS) -> list[dict]:
    """列回收站（顺带做过期清理）。"""
    auto_purge(days)
    out: list[dict] = []
    now = time.time()
    for path in sorted(trash_dir().glob("*" + SUFFIX), reverse=True):
        d = _read(path)
        if not d:
            continue
        ts = float(d.get("deleted_at") or 0)
        out.append({
            "entry": path.name,
            "id": d.get("id"),
            "title": d.get("title") or "(无标题)",
            "deleted_at": ts,
            "deleted_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
            "segments": d.get("segments_count") or 0,
            "has_summary": bool(d.get("has_summary")),
            "size": path.stat().st_size,
            "days_left": max(0, round((ts + days * 86400 - now) / 86400, 1)) if ts else 0,
        })
    return out


def get_item(name: str) -> dict | None:
    name = _safe_name(name)
    path = trash_dir() / name
    return _read(path) if path.exists() else None


# ---------------------------------------------------------------- 恢复 / 清除
def restore(store, name: str) -> dict:
    name = _safe_name(name)
    path = trash_dir() / name
    d = _read(path) if path.exists() else None
    if not d:
        return {"ok": False, "error": "回收站里没有这一项"}

    data = d.get("data") or {}
    old_id = (data.get("meeting") or {}).get("id") or d.get("id")
    existed = bool(store.get_meeting(old_id)) if old_id else False
    new_id = store.restore_snapshot(data)
    if not new_id:
        return {"ok": False, "error": "快照已损坏"}

    try:
        path.unlink()
    except OSError:
        pass
    return {"ok": True, "meeting_id": new_id,
            "renamed": existed or (old_id and new_id != old_id)}


def purge(name: str) -> dict:
    name = _safe_name(name)
    path = trash_dir() / name
    if not path.exists():
        return {"ok": False, "error": "回收站里没有这一项"}
    path.unlink()
    return {"ok": True}


def empty() -> dict:
    n = 0
    for path in trash_dir().glob("*" + SUFFIX):
        try:
            path.unlink()
            n += 1
        except OSError:
            pass
    return {"ok": True, "removed": n}


def auto_purge(days: int = DEFAULT_DAYS) -> list[str]:
    """删掉超过 days 天的快照，返回被删文件名。"""
    if days is None or days <= 0:
        return []
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    for path in trash_dir().glob("*" + SUFFIX):
        try:
            # 文件名前缀是毫秒时间戳，直接用它判断，不必解析 JSON
            stamp = float(path.name.split("-", 1)[0]) / 1000.0
        except (ValueError, IndexError):
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
        if stamp < cutoff:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed