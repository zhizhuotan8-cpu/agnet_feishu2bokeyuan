from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agents.evaluator import (
    build_feedback,
    evaluate,
    evaluate_safely,
    fallback_evaluation,
)
from app.agents.llm import LLMError

MARKDOWN = "# 标题\n\n这是正文，用来核对引用。"


def _settings(pass_score: float = 8.0) -> Any:
    return SimpleNamespace(
        pass_score=pass_score,
        use_fake_llm=True,
        effective_judge_model="judge",
        request_timeout=10.0,
        llm_model="stub",
        llm_base_url="http://stub/v1",
        llm_api_key="stub",
    )


class StubLLM:
    model = "stub"

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def chat_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload


def _payload(quote: str = "这是正文，用来核对引用。") -> dict[str, Any]:
    return {
        "scores": {
            "accuracy": 8,
            "specificity": 8,
            "density": 8,
            "structure": 8,
            "readability": 8,
        },
        "deductions": [
            {
                "dimension": "specificity",
                "points": 1,
                "quote": quote,
                "reason": "例子不够具体",
            }
        ],
        "veto": None,
    }


def test_normal_evaluation_computes_total_and_verdict() -> None:
    result = evaluate(StubLLM(_payload()), MARKDOWN, _settings())
    assert result.total == 8.0
    assert result.verdict == "pass"
    assert result.deductions[0].quote_verified is True
    assert result.error is None


def test_fabricated_quote_is_not_verified() -> None:
    result = evaluate(StubLLM(_payload(quote="这句话正文里没有")), MARKDOWN, _settings())
    assert result.deductions[0].quote_verified is False


def test_fallback_reports_error_not_veto() -> None:
    result = fallback_evaluation("调用模型失败：模型返回空内容")
    assert result.verdict == "below_bar"
    assert result.total == 0.0
    assert result.veto is None
    assert result.error == "调用模型失败：模型返回空内容"
    assert set(result.scores) == {"accuracy", "specificity", "density", "structure", "readability"}
    assert all(value == 0 for value in result.scores.values())


def test_build_feedback_carries_suggestions_and_deductions() -> None:
    payload = _payload()
    payload["suggestions"] = ["补一个能跑的耗时对比脚本"]
    feedback = build_feedback(payload, total=7.0)

    assert "上一版得分 7.0/10" in feedback
    assert "补一个能跑的耗时对比脚本" in feedback
    # 扣分项要翻成中文维度名，模型才看得懂是在说哪一块
    assert "具体性 -1" in feedback
    assert "例子不够具体" in feedback
    assert "输出完整 JSON" in feedback


def test_build_feedback_without_details_still_actionable() -> None:
    feedback = build_feedback({"scores": {}, "suggestions": []}, total=0.0)
    assert "没有拿到具体建议" in feedback
    assert "输出完整 JSON" in feedback


def test_evaluate_safely_swallows_llm_error() -> None:
    llm = StubLLM(error=LLMError("模型只返回了推理过程（900 字符）"))
    result = evaluate_safely(llm, MARKDOWN, _settings())
    assert llm.calls == 1
    assert result.veto is None
    assert "模型只返回了推理过程" in (result.error or "")
    assert result.verdict == "below_bar"
