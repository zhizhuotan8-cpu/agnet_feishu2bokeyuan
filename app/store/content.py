from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import Settings


def draft_markdown_path(settings: Settings, draft_id: str) -> Path:
    return settings.data_dir / "drafts" / f"{draft_id}.md"


def write_draft_markdown(settings: Settings, draft_id: str, markdown: str) -> Path:
    path = draft_markdown_path(settings, draft_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def read_draft_markdown(settings: Settings, draft_id: str) -> str:
    path = draft_markdown_path(settings, draft_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_dir(settings: Settings, note_id: str) -> Path:
    path = settings.data_dir / "notes" / note_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_note_snapshot(settings: Settings, note_id: str, markdown: str) -> tuple[Path, str]:
    """写一份冻结快照，返回 (路径, 内容 hash)。"""
    digest = sha256_text(markdown)
    path = note_dir(settings, note_id) / "note.md"
    path.write_text(markdown, encoding="utf-8")
    snapshots = settings.data_dir / "snapshots" / note_id
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{digest}.md").write_text(markdown, encoding="utf-8")
    return path, digest
