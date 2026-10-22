from __future__ import annotations

from pathlib import Path

from app.store.db import (
    add_event,
    create_draft,
    create_note,
    get_draft,
    get_note_by_draft,
    increment_rewrite,
    init_db,
    latest_evaluation,
    list_events,
    save_evaluation,
    set_status,
)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    init_db(path)
    return path


def test_create_and_get_draft(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="GIL 是什么", template_version="blog_v1")
    assert draft["status"] == "awaiting_review"
    assert draft["rewrite_count"] == 0
    assert get_draft(db, draft["draft_id"])["raw_point"] == "GIL 是什么"


def test_set_status_is_conditional(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    did = draft["draft_id"]

    assert set_status(db, did, "approved", expected="awaiting_review") is True
    # 重复点击：状态已经不是 awaiting_review，更新失败
    assert set_status(db, did, "approved", expected="awaiting_review") is False
    assert set_status(db, did, "written", expected="approved") is True
    assert get_draft(db, did)["status"] == "written"


def test_set_status_rejects_unknown_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    try:
        set_status(db, draft["draft_id"], "publishing")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知状态应该抛 ValueError")


def test_increment_rewrite(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    assert increment_rewrite(db, draft["draft_id"]) == 1
    assert increment_rewrite(db, draft["draft_id"]) == 2


def test_save_evaluation_keeps_detail(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    save_evaluation(
        db,
        draft_id=draft["draft_id"],
        scores={"accuracy": 8},
        total=8.0,
        judge_model="judge",
        prompt_version="rubric_v1",
        detail={"veto": None, "deductions": [{"quote": "原文"}]},
    )
    row = latest_evaluation(db, draft["draft_id"])
    assert row["total"] == 8.0
    assert '"quote"' in row["detail_json"]


def test_note_is_unique_per_draft(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    create_note(
        db,
        note_id="n_1",
        draft_id=draft["draft_id"],
        doc_token="doc_a",
        title="t",
        doc_url=None,
        local_path="/tmp/a.md",
        content_hash="abc",
    )
    assert get_note_by_draft(db, draft["draft_id"])["doc_token"] == "doc_a"


def test_events_are_ordered(tmp_path: Path) -> None:
    db = _db(tmp_path)
    draft = create_draft(db, raw_point="x", template_version="blog_v1")
    add_event(db, draft_id=draft["draft_id"], type_="first", payload={})
    add_event(db, draft_id=draft["draft_id"], type_="second", payload={"n": 1})
    events = list_events(db, draft["draft_id"])
    assert [e["type"] for e in events] == ["first", "second"]
