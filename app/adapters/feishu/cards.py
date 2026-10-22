"""审阅卡片：把评分结果拼成一张手机一屏能看完的交互卡片。

按钮的 value 里带 draft_id 和 action，回调端（feishu_listener）只认这两个字段，
其余展示内容随便改都不影响动作。
"""

from __future__ import annotations

import re
from typing import Any

DIMENSION_LABELS = {
    "accuracy": "准确性",
    "specificity": "具体性",
    "density": "信息密度",
    "structure": "结构",
    "readability": "可读性",
}

ACTION_LABELS = {"approve": "通过写入", "rewrite": "重写一次", "reject": "丢弃"}

_WHITESPACE = re.compile(r"\s+")


def _one_line(text: Any, limit: int = 160) -> str:
    value = _WHITESPACE.sub(" ", str(text or "")).strip()
    return value[:limit] + "…" if len(value) > limit else value


def _score_line(evaluation: dict[str, Any]) -> str:
    scores = evaluation.get("scores") or {}
    parts = []
    for key, label in DIMENSION_LABELS.items():
        if key in scores:
            parts.append(f"{label} {scores[key]}")
    return " · ".join(parts)


def build_review_card(
    payload: dict[str, Any], *, review_url: str | None = None
) -> dict[str, Any]:
    evaluation = payload.get("evaluation") or {}
    score = payload.get("score")
    passed = payload.get("verdict") == "pass"
    score_text = f"{float(score):.1f}" if score is not None else "—"
    title = _one_line(payload.get("title") or "未命名草稿", 80)
    draft_id = str(payload.get("draft_id") or "")
    rewrite_count = int(payload.get("rewrite_count") or 0)
    max_rewrites = int(payload.get("max_rewrites") or 3)

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            # 标题来自模型，用 plain_text 渲染，别让它把卡片 markdown 当指令执行
            "text": {
                "tag": "plain_text",
                "content": f"{title}\n草稿 {draft_id} · 重写 {rewrite_count}/{max_rewrites} 次",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{score_text}/10 {'达标' if passed else '未达标'}**\n"
                + (_score_line(evaluation) or "评估明细缺失"),
            },
        },
    ]

    error = _one_line(evaluation.get("error"), 200)
    veto = _one_line(evaluation.get("veto"), 200)
    if error:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='red'>评估失败：{error}</font>"}}
        )
    if veto:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='red'>一票否决：{veto}</font>"}}
        )

    deductions = [item for item in (evaluation.get("deductions") or []) if isinstance(item, dict)]
    if deductions:
        lines = ["**扣分项**"]
        for item in deductions[:3]:
            label = DIMENSION_LABELS.get(str(item.get("dimension")), str(item.get("dimension")))
            reason = _one_line(item.get("reason"), 100)
            lines.append(f"• {label} -{item.get('points', 0)}：{reason}")
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )

    excerpt = _one_line(payload.get("excerpt"), 300)
    if excerpt:
        elements.append({"tag": "hr"})
        # 正文用 plain_text，避免模型写的内容被当成卡片 markdown 执行
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": excerpt}})

    actions: list[dict[str, Any]] = []
    if review_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "看全文"},
                "type": "default",
                "url": review_url,
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["approve"]},
            "type": "primary",
            "value": {"action": "approve", "draft_id": draft_id},
        }
    )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["rewrite"]},
            "type": "default",
            "disabled": rewrite_count >= max_rewrites,
            "value": {"action": "rewrite", "draft_id": draft_id},
        }
    )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["reject"]},
            "type": "danger",
            "value": {"action": "reject", "draft_id": draft_id},
        }
    )
    elements.append({"tag": "action", "actions": actions})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "只有「待审阅」状态可以操作；重复点击不会重复写入。",
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "green" if passed else "red",
            "title": {"tag": "plain_text", "content": f"笔记草稿 · {score_text}/10"},
        },
        "elements": elements,
    }
