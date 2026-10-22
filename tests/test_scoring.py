from __future__ import annotations

import pytest

from app.agents.scoring import (
    VETO_CAP,
    WEIGHTS,
    clamp_score,
    compute_total,
    verdict_for,
    verify_quotes,
)


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_weighted_total() -> None:
    scores = {"accuracy": 9, "specificity": 8, "density": 7, "structure": 8, "readability": 6}
    expected = 9 * 0.3 + 8 * 0.2 + 7 * 0.2 + 8 * 0.15 + 6 * 0.15
    assert compute_total(scores) == pytest.approx(round(expected, 1))


def test_all_tens() -> None:
    everywhere = {name: 10 for name in WEIGHTS}
    assert compute_total(everywhere) == 10.0


def test_veto_caps_total() -> None:
    everywhere = {name: 10 for name in WEIGHTS}
    assert compute_total(everywhere, veto="把两个学派说反了") == VETO_CAP


def test_veto_does_not_raise_low_score() -> None:
    low = {name: 3 for name in WEIGHTS}
    assert compute_total(low, veto="事实错误") == 3.0


def test_verdict_threshold_is_inclusive() -> None:
    assert verdict_for(8.0, 8.0) == "pass"
    assert verdict_for(7.9, 8.0) == "below_bar"


def test_clamp_score() -> None:
    assert clamp_score(11) == 10
    assert clamp_score(-2) == 0
    assert clamp_score("7") == 7
    assert clamp_score(None) == 0
    assert clamp_score("abc") == 0


def test_verify_quotes_normalizes_whitespace() -> None:
    markdown = "第一段 有 空格\n\n第二段。"
    checked = verify_quotes(
        markdown,
        [
            {"quote": "第一段 有 空格", "points": 1},
            {"quote": "第一段  有   空格", "points": 1},
            {"quote": "这句话不在正文里", "points": 1},
            {"quote": "", "points": 1},
        ],
    )
    assert [row["quote_verified"] for row in checked] == [True, True, False, False]


def test_compute_total_requires_all_dimensions() -> None:
    with pytest.raises(ValueError):
        compute_total({"accuracy": 8})
