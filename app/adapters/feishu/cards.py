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

    if payload.get("fallback_to_best"):
        # 明确告诉人「这不是最后一版，是最好的一版」。通过后又手动重写、分数反而
        # 掉了的情况也会走到这里，措辞要区分开
        attempts = int(payload.get("attempts") or 0)
        attempt = payload.get("best_attempt")
        where = f"（第 {attempt} 版）" if attempt else ""
        if passed:
            line = f"**重写后的版本分数更低，这里展示之前达标的一版{where}**"
        else:
            line = f"**{attempts} 个版本都没过线，这里展示得分最高的一版{where}**"
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": line}}
        )

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
    # value 里带上 rewrite_count：重写后旧卡片上的按钮会因为计数对不上被判过期，
    # 否则点历史卡片的「通过」会作用到新版本上
    base_value = {"draft_id": draft_id, "rewrite_count": rewrite_count}
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["approve"]},
            "type": "primary",
            "value": {"action": "approve", **base_value},
        }
    )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["rewrite"]},
            "type": "default",
            "disabled": rewrite_count >= max_rewrites,
            "value": {"action": "rewrite", **base_value},
        }
    )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": ACTION_LABELS["reject"]},
            "type": "danger",
            "value": {"action": "reject", **base_value},
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


# 结果卡片的状态 → (标题, 头部颜色, 默认说明)
RESULT_STYLES = {
    "written": ("已写入飞书", "green", "文档已经建好，点下面的链接就能打开。"),
    "rejected": ("已丢弃", "grey", "这篇稿子没有写入飞书。"),
    "rewritten": (
        "已提交重写",
        "blue",
        "正在重新生成，完成后会往群里发一张新的审阅卡片。",
    ),
    "write_failed": (
        "写入失败",
        "orange",
        "文档没建成，草稿已回滚到待写入状态。修好原因后可以直接重试，不用重新生成。",
    ),
    "error": ("操作失败", "red", "这次操作没有完成，可以回命令行看详细错误。"),
}


def build_result_card(payload: dict[str, Any]) -> dict[str, Any]:
    """把审阅卡片换成结果卡片：去掉审阅按钮，避免同一张卡被反复点。

    payload 字段：status / draft_id / title / score / doc_url / error / rewrite_count / retryable。
    写入失败且草稿还停在 approved 时，带一个「重试写入」按钮——这正是最容易
    出错的一步（文件夹权限、导入任务），不该逼用户回命令行。
    """
    status = str(payload.get("status") or "error")
    title_text, color, default_line = RESULT_STYLES.get(status, RESULT_STYLES["error"])
    title = _one_line(payload.get("title") or "未命名草稿", 80)
    draft_id = str(payload.get("draft_id") or "")
    score = payload.get("score")
    score_text = f"{float(score):.1f}" if score is not None else "—"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"{title}\n草稿 {draft_id} · {score_text}/10",
            },
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{title_text}**\n{default_line}"},
        },
    ]

    error = _one_line(payload.get("error"), 300)
    if error:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"<font color='red'>原因：{error}</font>",
                },
            }
        )

    doc_url = str(payload.get("doc_url") or "").strip()
    if doc_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开文档"},
                        "type": "primary",
                        "url": doc_url,
                    }
                ],
            }
        )

    if payload.get("retryable"):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "重试写入"},
                        "type": "primary",
                        "value": {
                            "action": "approve",
                            "draft_id": draft_id,
                            "rewrite_count": int(payload.get("rewrite_count") or 0),
                        },
                    }
                ],
            }
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "这张卡片已经处理过了，原来的审阅按钮不再有效。",
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"笔记草稿 · {title_text}"},
        },
        "elements": elements,
    }
