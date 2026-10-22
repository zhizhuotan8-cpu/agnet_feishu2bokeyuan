from __future__ import annotations

from app.adapters.feishu.cards import build_result_card, build_review_card


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


def test_card_flags_best_version_when_all_attempts_miss() -> None:
    card = build_review_card(
        _payload(
            score=7.0,
            verdict="below_bar",
            rewrite_count=2,
            attempts=3,
            fallback_to_best=True,
            best_attempt=1,
        )
    )
    texts = " ".join(
        str(element.get("text", {}).get("content", "")) for element in card["elements"]
    )
    assert "3 个版本都没过线" in texts
    assert "第 1 版" in texts


def _result_payload(**overrides) -> dict:
    payload = {
        "status": "written",
        "draft_id": "d_1",
        "title": "Python 的 GIL",
        "score": 8.6,
        "doc_url": "https://example.feishu.cn/docx/docx_1",
        "error": None,
        "rewrite_count": 1,
        "retryable": False,
    }
    payload.update(overrides)
    return payload


def _result_text(card: dict) -> str:
    parts = []
    for element in card["elements"]:
        if element.get("text"):
            parts.append(str(element["text"].get("content", "")))
        for button in element.get("actions") or []:
            parts.append(str(button.get("text", {}).get("content", "")))
    return " ".join(parts)


def test_result_card_swaps_out_review_buttons() -> None:
    card = build_result_card(_result_payload())
    # 结果卡片上不能再有带 action 的按钮，否则同一张卡会被反复点
    assert _buttons_optional(card) == {}
    assert card["header"]["template"] == "green"
    assert "已写入飞书" in _result_text(card)

    links = [
        button
        for element in card["elements"]
        for button in (element.get("actions") or [])
        if button.get("url")
    ]
    assert [b["url"] for b in links] == ["https://example.feishu.cn/docx/docx_1"]


def test_result_card_retry_button_carries_rewrite_count() -> None:
    card = build_result_card(
        _result_payload(
            status="write_failed",
            doc_url=None,
            error="导入失败：文件夹无权限",
            retryable=True,
        )
    )
    buttons = _buttons(card)
    assert list(buttons) == ["approve"]
    assert buttons["approve"]["text"]["content"] == "重试写入"
    assert buttons["approve"]["value"]["draft_id"] == "d_1"
    assert buttons["approve"]["value"]["rewrite_count"] == 1

    text = _result_text(card)
    assert "写入失败" in text
    assert "文件夹无权限" in text


def test_result_card_rewritten_points_to_new_card() -> None:
    card = build_result_card(_result_payload(status="rewritten", doc_url=None))
    assert card["header"]["template"] == "blue"
    assert "新的审阅卡片" in _result_text(card)
    assert _buttons_optional(card) == {}


def _buttons_optional(card: dict) -> dict[str, dict]:
    buttons = {}
    for element in card["elements"]:
        for button in element.get("actions") or []:
            if "value" in button:
                buttons[button["value"]["action"]] = button
    return buttons
