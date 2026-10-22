from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
  draft_id         TEXT PRIMARY KEY,
  raw_point        TEXT NOT NULL,
  markdown         TEXT,
  content_json     TEXT,
  template_version TEXT NOT NULL,
  model            TEXT,
  prompt_version   TEXT,
  score            REAL,
  verdict          TEXT,
  status           TEXT NOT NULL,
  review_msg_id    TEXT,
  rewrite_count    INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id       TEXT NOT NULL,
  scores_json    TEXT NOT NULL,
  detail_json    TEXT,
  total          REAL NOT NULL,
  judge_model    TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
  note_id      TEXT PRIMARY KEY,
  draft_id     TEXT UNIQUE,
  doc_token    TEXT NOT NULL UNIQUE,
  title        TEXT NOT NULL,
  doc_url      TEXT,
  local_path   TEXT,
  content_hash TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
  plan_id      TEXT PRIMARY KEY,
  note_id      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id      TEXT NOT NULL,
  platform     TEXT NOT NULL,
  post_id      TEXT,
  url          TEXT,
  content_hash TEXT NOT NULL,
  status       TEXT NOT NULL,
  error        TEXT,
  published_at TEXT,
  UNIQUE(note_id, platform)
);

CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id     TEXT,
  type         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""

# 草稿状态流转（括号里是中文含义，状态值本身要跟代码/卡片保持一致，不翻译）：
# awaiting_review（待审阅）-> approved（已通过）-> creating_doc（建文档中）-> written（已写入）
#                        \-> rejected（已拒绝）/ discarded（已作废）
DRAFT_STATUSES = (
    "awaiting_review",
    "approved",
    "creating_doc",
    "written",
    "rejected",
    "discarded",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create_draft(
    db_path: Path,
    *,
    raw_point: str,
    template_version: str,
    draft_id: str | None = None,
) -> dict[str, Any]:
    draft_id = draft_id or f"d_{uuid.uuid4().hex[:10]}"
    ts = now_iso()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, raw_point, template_version, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'awaiting_review', ?, ?)",
            (draft_id, raw_point, template_version, ts, ts),
        )
        conn.commit()
    return get_draft(db_path, draft_id)  # type: ignore[return-value]


def get_draft(db_path: Path, draft_id: str) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_drafts(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_draft(db_path: Path, draft_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "markdown",
        "content_json",
        "model",
        "prompt_version",
        "score",
        "verdict",
        "status",
        "review_msg_id",
        "rewrite_count",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"不可更新的字段: {unknown}")
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [draft_id]
    with closing(_connect(db_path)) as conn:
        conn.execute(
            f"UPDATE drafts SET {assignments} WHERE draft_id = ?",  # noqa: S608 - 字段名来自白名单
            values,
        )
        conn.commit()


def set_status(
    db_path: Path, draft_id: str, status: str, *, expected: str | None = None
) -> bool:
    """条件更新：只有当前状态等于 expected 时才改，用来挡重复点击。"""
    if status not in DRAFT_STATUSES:
        raise ValueError(f"未知状态: {status}")
    sql = "UPDATE drafts SET status = ?, updated_at = ? WHERE draft_id = ?"
    params: list[Any] = [status, now_iso(), draft_id]
    if expected is not None:
        sql += " AND status = ?"
        params.append(expected)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount == 1


def increment_rewrite(db_path: Path, draft_id: str) -> int:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE drafts SET rewrite_count = rewrite_count + 1, updated_at = ? WHERE draft_id = ?",
            (now_iso(), draft_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rewrite_count FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    return int(row["rewrite_count"]) if row else 0


def save_evaluation(
    db_path: Path,
    *,
    draft_id: str,
    scores: dict[str, int],
    total: float,
    judge_model: str,
    prompt_version: str,
    detail: dict[str, Any] | None = None,
) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO evaluations"
            " (draft_id, scores_json, detail_json, total, judge_model, prompt_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                json.dumps(scores, ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False) if detail else None,
                total,
                judge_model,
                prompt_version,
                now_iso(),
            ),
        )
        conn.commit()


def latest_evaluation(db_path: Path, draft_id: str) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE draft_id = ? ORDER BY id DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
    return _row_to_dict(row)


def create_note(
    db_path: Path,
    *,
    note_id: str,
    draft_id: str,
    doc_token: str,
    title: str,
    doc_url: str | None,
    local_path: str | None,
    content_hash: str,
) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO notes (note_id, draft_id, doc_token, title, doc_url, local_path, content_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note_id,
                draft_id,
                doc_token,
                title,
                doc_url,
                local_path,
                content_hash,
                now_iso(),
            ),
        )
        conn.commit()


def get_note_by_draft(db_path: Path, draft_id: str) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM notes WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    return _row_to_dict(row)


def add_event(
    db_path: Path, *, draft_id: str | None, type_: str, payload: dict[str, Any]
) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO events (draft_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                draft_id,
                type_,
                json.dumps(payload, ensure_ascii=False, default=str),
                now_iso(),
            ),
        )
        conn.commit()


def list_events(db_path: Path, draft_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE draft_id = ? ORDER BY id ASC LIMIT ?",
            (draft_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
