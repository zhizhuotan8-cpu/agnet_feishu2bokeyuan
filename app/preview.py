from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from markdown_it import MarkdownIt

from app.agents.scoring import WEIGHTS
from app.agents.schemas import TEMPLATE_VERSION
from app.config import Settings
from app.store.content import read_draft_markdown
from app.store.db import get_draft, get_note_by_draft, latest_evaluation

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

DIMENSION_LABELS = {
    "accuracy": "准确性",
    "specificity": "具体性",
    "density": "信息密度",
    "structure": "结构",
    "readability": "可读性",
}

STATUS_LABELS = {
    "awaiting_review": "待审阅",
    "approved": "已通过，待写入",
    "creating_doc": "写入中",
    "written": "已写入飞书",
    "rejected": "已拒绝",
    "discarded": "已丢弃",
}


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # 只渲染预览页；标题、扣分理由都来自模型，必须默认转义。
        # 正文是 markdown-it(html=False) 生成的，下面单独用 |safe 放行。
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@lru_cache
def _markdown() -> MarkdownIt:
    # html=False：模型输出里的原始 HTML 不会被执行，只当普通文本
    return MarkdownIt("commonmark", {"html": False, "breaks": False}).enable("table")


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_view(settings: Settings, draft_id: str) -> dict[str, Any]:
    draft = get_draft(settings.db_path, draft_id)
    if draft is None:
        raise LookupError(f"草稿不存在: {draft_id}")

    evaluation_row = latest_evaluation(settings.db_path, draft_id) or {}
    scores = _loads(evaluation_row.get("scores_json"))
    detail = _loads(evaluation_row.get("detail_json"))

    note = get_note_by_draft(settings.db_path, draft_id) or {}
    content = _loads(draft.get("content_json"))
    markdown = draft.get("markdown") or read_draft_markdown(settings, draft_id)
    score = float(draft.get("score") or evaluation_row.get("total") or 0.0)
    verdict = draft.get("verdict") or detail.get("verdict") or "below_bar"

    dimensions = [
        {
            "key": key,
            "label": DIMENSION_LABELS.get(key, key),
            "score": int(scores.get(key) or 0),
            "weight": WEIGHTS[key],
        }
        for key in WEIGHTS
    ]

    deductions = [
        {
            "dimension_label": DIMENSION_LABELS.get(
                str(item.get("dimension")), str(item.get("dimension"))
            ),
            "points": float(item.get("points") or 0),
            "quote": str(item.get("quote") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "quote_verified": bool(item.get("quote_verified", True)),
        }
        for item in (detail.get("deductions") or [])
        if isinstance(item, dict)
    ]

    return {
        "draft_id": draft_id,
        "title": content.get("title") or "（没有标题）",
        "score": score,
        "pass_score": settings.pass_score,
        "passed": score >= settings.pass_score and verdict == "pass",
        "verdict": verdict,
        "status": draft["status"],
        "status_label": STATUS_LABELS.get(draft["status"], draft["status"]),
        "rewrite_count": int(draft.get("rewrite_count") or 0),
        "max_rewrites": settings.max_rewrites,
        "can_review": draft["status"] == "awaiting_review",
        "dimensions": dimensions,
        "deductions": deductions,
        "veto": (detail.get("veto") or "").strip() or None,
        "error": (detail.get("error") or "").strip() or None,
        "uncertain_claims": content.get("uncertain_claims") or [],
        "confidence_label": {"high": "高", "medium": "中", "low": "低"}.get(
            str(content.get("confidence") or ""), "未标注"
        ),
        "body": _markdown().render(markdown),
        "doc_url": note.get("doc_url"),
        "note_id": note.get("note_id"),
        "template_version": draft.get("template_version") or TEMPLATE_VERSION,
        "updated_at": draft.get("updated_at"),
        "static": False,
    }


def render_preview(
    settings: Settings, draft_id: str, *, static: bool = False
) -> str:
    view = build_view(settings, draft_id)
    view["static"] = static
    return _env().get_template("preview.html.j2").render(**view)


def write_static_preview(settings: Settings, draft_id: str) -> Path:
    path = settings.data_dir / "previews" / f"{draft_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_preview(settings, draft_id, static=True), encoding="utf-8")
    return path


__all__ = [
    "DIMENSION_LABELS",
    "STATUS_LABELS",
    "build_view",
    "render_preview",
    "write_static_preview",
]
