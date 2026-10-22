from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.preview import build_view, render_preview, write_static_preview
from app.store.db import (
    create_draft,
    get_draft,
    init_db,
    save_evaluation,
    update_draft,
)


def _draft_with_content(settings: Settings) -> str:
    draft = create_draft(
        settings.db_path, raw_point="预览测试", template_version="blog_v1"
    )
    draft_id = draft["draft_id"]
    content = {
        "title": "标题里带 <script>alert(1)</script>",
        "uncertain_claims": ["这一点还没核实"],
    }
    markdown = (
        "# 标题里带 <script>alert(2)</script>\n\n"
        "正文段落。\n\n"
        "```python\nprint('hi')\n```\n"
    )
    update_draft(
        settings.db_path,
        draft_id,
        markdown=markdown,
        content_json=json.dumps(content, ensure_ascii=False),
        score=7.0,
        verdict="below_bar",
        model="fake",
    )
    (settings.data_dir / "drafts").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "drafts" / f"{draft_id}.md").write_text(markdown, encoding="utf-8")

    save_evaluation(
        settings.db_path,
        draft_id=draft_id,
        scores={"accuracy": 7, "specificity": 6, "density": 8, "structure": 8, "readability": 8},
        total=7.0,
        judge_model="fake-llm",
        prompt_version="rubric_v1",
        detail={
            "veto": None,
            "deductions": [
                {
                    "dimension": "specificity",
                    "points": 1.0,
                    "quote": "正文段落。",
                    "reason": "没有具体例子。",
                    "quote_verified": True,
                }
            ],
        },
    )
    return draft_id


def test_build_view_shape(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    draft_id = _draft_with_content(settings)

    view = build_view(settings, draft_id)
    assert view["score"] == 7.0
    assert view["passed"] is False
    assert view["can_review"] is True
    assert len(view["dimensions"]) == 5
    assert view["dimensions"][0]["label"] == "准确性"
    assert view["deductions"][0]["dimension_label"] == "具体性"
    assert view["uncertain_claims"] == ["这一点还没核实"]


def test_preview_escapes_model_text(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    draft_id = _draft_with_content(settings)

    html = render_preview(settings, draft_id)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # 标题被转义
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html  # 正文原始 HTML 也被转义
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert(2)</script>" not in html
    assert "没有具体例子。" in html


def test_static_preview_disables_buttons(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    draft_id = _draft_with_content(settings)

    path = write_static_preview(settings, draft_id)
    html = path.read_text(encoding="utf-8")
    assert path.name == f"{draft_id}.html"
    assert "静态预览" in html
    assert "onclick=\"review('approve')\"" not in html


def test_status_after_write_hides_buttons(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    draft_id = _draft_with_content(settings)
    update_draft(settings.db_path, draft_id, status="written")

    view = build_view(settings, draft_id)
    assert view["can_review"] is False
    assert "已写入飞书" in render_preview(settings, draft_id)


def test_missing_draft_raises(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    try:
        build_view(settings, "d_nope")
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("不存在的草稿应该抛 LookupError")


def test_get_draft_roundtrip(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", fake_llm=True)
    settings.ensure_dirs()
    init_db(settings.db_path)
    draft_id = _draft_with_content(settings)
    assert get_draft(settings.db_path, draft_id)["verdict"] == "below_bar"
