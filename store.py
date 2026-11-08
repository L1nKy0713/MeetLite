"""SQLite 存储：会议 / 转写片段 / 摘要。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'idle',
  meta TEXT
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  lang TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seg_meeting ON segments(meeting_id, start_ms);
CREATE TABLE IF NOT EXISTS summaries(
  meeting_id TEXT PRIMARY KEY,
  template TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes(
  meeting_id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------------- meetings ----------------
    def create_meeting(self, title: str | None = None) -> dict:
        now = time.time()
        mid = uuid.uuid4().hex[:12]
        title = title or time.strftime("会议 %Y-%m-%d %H:%M")
        with self._lock:
            self._conn.execute(
                "INSERT INTO meetings(id,title,created_at,updated_at,status) VALUES(?,?,?,?,?)",
                (mid, title, now, now, "idle"),
            )
            self._conn.commit()
        return self.get_meeting(mid)

    def get_meeting(self, mid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None

    def list_meetings(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.*, (SELECT COUNT(*) FROM segments s WHERE s.meeting_id=m.id) AS seg_count "
                "FROM meetings m ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_meeting(self, mid: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE meetings SET {cols} WHERE id=?", (*fields.values(), mid))
            self._conn.commit()

    def delete_meeting(self, mid: str) -> None:
        with self._lock:
            for t in ("segments", "summaries", "notes", "meetings"):
                self._conn.execute(f"DELETE FROM {t} WHERE {'id' if t == 'meetings' else 'meeting_id'}=?", (mid,))
            self._conn.commit()

    # ---------------- 快照（回收站用） ----------------
    def snapshot(self, mid: str) -> dict | None:
        """把一条会议的全部数据导成纯 dict，供回收站落盘。"""
        mt = self.get_meeting(mid)
        if not mt:
            return None
        return {
            "meeting": mt,
            "segments": self.segments(mid),
            "summary": self.get_summary(mid),
            "notes": self.get_notes(mid),
        }

    def restore_snapshot(self, snap: dict) -> str | None:
        """把快照写回库。原 id 已被占用时分配新 id。返回最终 id。"""
        if not isinstance(snap, dict):
            return None
        mt = snap.get("meeting") or {}
        if not mt.get("title"):
            return None
        mid = str(mt.get("id") or "").strip()
        if not mid or self.get_meeting(mid):
            mid = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO meetings(id,title,created_at,updated_at,duration_ms,status,meta)"
                " VALUES(?,?,?,?,?,?,?)",
                (mid, mt.get("title") or "(无标题)",
                 float(mt.get("created_at") or now), float(mt.get("updated_at") or now),
                 int(mt.get("duration_ms") or 0), mt.get("status") or "done", mt.get("meta")),
            )
            for s in snap.get("segments") or []:
                self._conn.execute(
                    "INSERT INTO segments(meeting_id,start_ms,end_ms,text,lang,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (mid, int(s.get("start_ms") or 0), int(s.get("end_ms") or 0),
                     s.get("text") or "", s.get("lang"), float(s.get("created_at") or now)),
                )
            sm = snap.get("summary") or {}
            if sm.get("content"):
                self._conn.execute(
                    "INSERT OR REPLACE INTO summaries(meeting_id,template,content,created_at)"
                    " VALUES(?,?,?,?)",
                    (mid, sm.get("template") or "standard_meeting", sm["content"],
                     float(sm.get("created_at") or now)),
                )
            notes = snap.get("notes")
            if notes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO notes(meeting_id,content,updated_at) VALUES(?,?,?)",
                    (mid, notes, now),
                )
            self._conn.commit()
        return mid

    # ---------------- segments ----------------
    def add_segment(self, mid: str, start_ms: int, end_ms: int, text: str, lang: str | None) -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO segments(meeting_id,start_ms,end_ms,text,lang,created_at) VALUES(?,?,?,?,?,?)",
                (mid, start_ms, end_ms, text, lang, now),
            )
            self._conn.execute("UPDATE meetings SET updated_at=?, duration_ms=MAX(duration_ms,?) WHERE id=?",
                               (now, end_ms, mid))
            self._conn.commit()
            sid = cur.lastrowid
        return {"id": sid, "meeting_id": mid, "start_ms": start_ms, "end_ms": end_ms, "text": text}

    def segments(self, mid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE meeting_id=? ORDER BY start_ms, id", (mid,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- summary / notes ----------------
    def save_summary(self, mid: str, template: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries(meeting_id,template,content,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(meeting_id) DO UPDATE SET template=excluded.template, content=excluded.content, created_at=excluded.created_at",
                (mid, template, content, time.time()),
            )
            self._conn.commit()

    def get_summary(self, mid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM summaries WHERE meeting_id=?", (mid,)).fetchone()
        return dict(r) if r else None

    def save_notes(self, mid: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes(meeting_id,content,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(meeting_id) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (mid, content, time.time()),
            )
            self._conn.commit()

    def get_notes(self, mid: str) -> str:
        with self._lock:
            r = self._conn.execute("SELECT content FROM notes WHERE meeting_id=?", (mid,)).fetchone()
        return r["content"] if r else ""

    def reset_recording_states(self) -> int:
        """进程上次异常退出时残留的 recording 状态，启动时清掉。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE meetings SET status='idle' WHERE status='recording'")
            self._conn.commit()
        return cur.rowcount

    def transcript_text(self, mid: str) -> str:
        segs = self.segments(mid)
        return "\n".join(f"[{fmt_ts(s['start_ms'])}] {s['text']}" for s in segs)


def fmt_ts(ms: int) -> str:
    s, ms = divmod(int(ms), 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"