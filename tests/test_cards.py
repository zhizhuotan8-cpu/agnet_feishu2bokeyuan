from __future__ import annotations

from app.adapters.feishu.cards import build_review_card


def _payload(**overrides) -> dict:
    payload = {
        "draft_id": "d_1",
        "title": "Python 的 GIL",
        "score": 8.0,
        "verdict": "pass",
        "pass_score": 8.0,
        "evaluation": {
            "scores": {
                "accuracy": 8,
                "specificity": 7,
                "density": 8,
                "structure": 9,
                "readability": 8,
            },
            "deductions": [
                {"dimension": "specificity", "points": 1, "reason": "缺少实测数据"},
            ],
            "veto": None,
            "error": None,
        },
        "excerpt": "这是一段正文预览 **不该被当成 markdown**",
        "rewrite_count": 0,
        "max_rewrites": 3,
    }
    payload.update(overrides)
    return payload


def _buttons(card: dict) -> dict[str, dict]:
    for element in card["elements"]:
        if element.get("tag") == "action":
            # 「看全文」是带 url 的链接按钮，没有 value，也不算审阅动作
            return {
                button["value"]["action"]: button
                for button in element["actions"]
                if "value" in button
            }
    raise AssertionError("卡片里没有按钮")


def test_card_has_three_actions_and_link() -> None:
    card = build_review_card(_payload(), review_url="http://127.0.0.1:8000/drafts/d_1")
    buttons = _buttons(card)
    assert set(buttons) == {"approve", "rewrite", "reject"}
    assert buttons["approve"]["value"]["draft_id"] == "d_1"
    assert buttons["approve"]["type"] == "primary"

    links = [
        element
        for element in card["elements"]
        if element.get("tag") == "action"
        and any(button.get("url") for button in element["actions"])
    ]
    assert links, "应该有一个「看全文」的链接按钮"


def test_header_color_follows_verdict() -> None:
    assert build_review_card(_payload())["header"]["template"] == "green"
    assert build_review_card(_payload(verdict="below_bar"))["header"]["template"] == "red"


def test_rewrite_disabled_at_limit() -> None:
    card = build_review_card(_payload(rewrite_count=3))
    assert _buttons(card)["rewrite"]["disabled"] is True


def test_excerpt_is_plain_text() -> None:
    card = build_review_card(_payload())
    plain = [e for e in card["elements"] if e.get("text", {}).get("tag") == "plain_text"]
    assert plain, "正文预览必须用 plain_text，避免模型内容被当作卡片 markdown 执行"
    assert any(
        "**不该被当成 markdown**" in e["text"]["content"] for e in plain
    )


def test_evaluator_error_is_shown_as_failure() -> None:
    payload = _payload()
    payload["evaluation"] = {
        "scores": {},
        "deductions": [],
        "veto": None,
        "error": "调用模型失败：模型返回空内容",
    }
    card = build_review_card(payload)
    texts = " ".join(
        str(element.get("text", {}).get("content", "")) for element in card["elements"]
    )
    assert "评估失败" in texts
    assert "一票否决" not in texts
