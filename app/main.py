from __future__ import annotations

import html
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import get_settings
from app.graph.runner import resume_note
from app.preview import STATUS_LABELS, render_preview
from app.store.db import list_drafts

app = FastAPI(title="飞书笔记 Agent", docs_url=None, redoc_url=None)


class ReviewIn(BaseModel):
    action: Literal["approve", "reject", "rewrite"]
    note: str | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    settings = get_settings()
    rows = list_drafts(settings.db_path, limit=50)

    if not rows:
        body = "<p class='empty'>还没有草稿。先运行 <code>note-agent new \"你的知识点\"</code>。</p>"
    else:
        items = []
        for row in rows:
            score = row.get("score")
            score_text = f"{float(score):.1f}" if score is not None else "—"
            status = STATUS_LABELS.get(row["status"], row["status"])
            title = html.escape((row.get("raw_point") or "")[:60])
            items.append(
                "<tr>"
                f"<td><a href='/drafts/{html.escape(row['draft_id'])}'>{html.escape(row['draft_id'])}</a></td>"
                f"<td class='score'>{score_text}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{title}</td>"
                f"<td class='muted'>{html.escape(row['created_at'])}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>草稿</th><th>分数</th><th>状态</th>"
            "<th>知识点</th><th>创建时间</th></tr></thead><tbody>"
            + "".join(items)
            + "</tbody></table>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>飞书笔记 Agent</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#f7f8fa; color:#1b1f24;
         font:15px/1.7 -apple-system,"Segoe UI","PingFang SC",sans-serif; }}
  main {{ max-width:900px; margin:0 auto; padding:32px 20px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  p.sub {{ color:#5b6470; margin:0 0 24px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff;
           border:1px solid #e4e7ec; border-radius:10px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #eef0f3; }}
  th {{ font-size:13px; color:#5b6470; background:#fbfbfc; }}
  tr:last-child td {{ border-bottom:none; }}
  a {{ color:#1c4ed8; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .score {{ font-variant-numeric: tabular-nums; font-weight:600; }}
  .muted {{ color:#5b6470; font-size:13px; }}
  .empty {{ background:#fff; border:1px dashed #d5dae1; padding:24px; border-radius:10px; }}
</style></head>
<body><main>
<h1>飞书笔记 Agent</h1>
<p class="sub">草稿列表 · 最近 50 条 · 只有「待审阅」状态可以点通过/拒绝</p>
{body}
</main></body></html>"""


@app.get("/drafts/{draft_id}", response_class=HTMLResponse)
def draft_page(draft_id: str) -> str:
    try:
        return render_preview(get_settings(), draft_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/drafts/{draft_id}/review")
def review(draft_id: str, body: ReviewIn) -> dict[str, str]:
    settings = get_settings()
    try:
        draft = resume_note(
            draft_id,
            body.action,
            note=body.note,
            settings=settings,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"draft_id": draft_id, "status": draft["status"]}
