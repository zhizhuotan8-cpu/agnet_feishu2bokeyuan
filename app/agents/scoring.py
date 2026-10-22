from __future__ import annotations

import re
from typing import Any, Iterable, Literal

WEIGHTS: dict[str, float] = {
    "accuracy": 0.30,
    "specificity": 0.20,
    "density": 0.20,
    "structure": 0.15,
    "readability": 0.15,
}

VETO_CAP = 5.0


def clamp_score(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(10, number))


def compute_total(scores: dict[str, Any], veto: str | None = None) -> float:
    missing = set(WEIGHTS) - set(scores)
    if missing:
        raise ValueError(f"缺少评分维度：{sorted(missing)}")

    total = sum(WEIGHTS[name] * clamp_score(scores[name]) for name in WEIGHTS)
    total = round(total, 1)
    if veto:
        total = min(total, VETO_CAP)
    return total


def verdict_for(total: float, pass_score: float) -> Literal["pass", "below_bar"]:
    return "pass" if total >= pass_score else "below_bar"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def verify_quotes(markdown: str, deductions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """扣分必须能在原文里逐字找到，找不到的标记出来，展示时忽略。"""
    haystack = _normalize(markdown)
    checked: list[dict[str, Any]] = []
    for item in deductions:
        row = dict(item)
        quote = _normalize(str(row.get("quote", "")))
        row["quote_verified"] = bool(quote) and quote in haystack
        checked.append(row)
    return checked
