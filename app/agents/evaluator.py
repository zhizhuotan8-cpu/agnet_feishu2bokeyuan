from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.llm import LLMClient, LLMError
from app.agents.prompts import EVALUATOR_PROMPT_VERSION, EVALUATOR_SYSTEM, EVALUATOR_USER
from app.agents.schemas import Deduction, Evaluation, TEMPLATE_VERSION
from app.agents.scoring import WEIGHTS, compute_total, verdict_for, verify_quotes
from app.config import Settings

logger = logging.getLogger(__name__)


def fallback_evaluation(reason: str) -> Evaluation:
    return Evaluation(
        scores={name: 0 for name in WEIGHTS},
        deductions=[],
        veto=None,
        error=reason,
        total=0.0,
        verdict="below_bar",
    )


def evaluate(llm: LLMClient, markdown: str, settings: Settings) -> Evaluation:
    raw = llm.chat_json(
        EVALUATOR_SYSTEM,
        EVALUATOR_USER.format(markdown=markdown),
        model=settings.effective_judge_model if not settings.use_fake_llm else None,
        temperature=0.0,
        # 扣分项要引用原文，900 tokens 容易被截断；而且现在空返回会退回普通文本重试
        max_tokens=1800,
    )
    evaluation = Evaluation.model_validate(raw)

    checked = verify_quotes(markdown, [d.model_dump() for d in evaluation.deductions])
    evaluation.deductions = [Deduction.model_validate(row) for row in checked]

    evaluation.total = compute_total(evaluation.scores, evaluation.veto)
    evaluation.verdict = verdict_for(evaluation.total, settings.pass_score)
    return evaluation


_DIMENSION_LABELS = {
    "accuracy": "准确性",
    "specificity": "具体性",
    "density": "信息密度",
    "structure": "结构",
    "readability": "可读性",
}


def build_feedback(evaluation: dict, *, total: float | None = None) -> str:
    """把评估结果转成下一轮重写能直接用的改稿说明。

    建议优先；没有建议时退回扣分项和否决原因，至少让模型知道哪里被扣了。
    """
    lines: list[str] = []
    if total is not None:
        lines.append(f"上一版得分 {float(total):.1f}/10，没有达到发布标准，请重写。")
    else:
        lines.append("上一版没有达到发布标准，请重写。")

    suggestions = [
        str(item).strip()
        for item in (evaluation.get("suggestions") or [])
        if str(item).strip()
    ]
    if suggestions:
        lines.append("修改建议：")
        lines.extend(f"- {item}" for item in suggestions[:5])

    deductions = [
        item for item in (evaluation.get("deductions") or []) if isinstance(item, dict)
    ]
    if deductions:
        lines.append("扣分项（按原样引用的是原文，不要照抄这些问题）：")
        for item in deductions[:6]:
            dimension = str(item.get("dimension") or "")
            label = _DIMENSION_LABELS.get(dimension, dimension)
            reason = str(item.get("reason") or "").strip()
            quote = str(item.get("quote") or "").strip()
            suffix = f"（原文：{quote[:80]}）" if quote else ""
            lines.append(f"- {label} -{item.get('points', 0)}：{reason}{suffix}")

    veto = str(evaluation.get("veto") or "").strip()
    if veto:
        lines.append(f"一票否决：{veto}")

    error = str(evaluation.get("error") or "").strip()
    if error:
        lines.append(f"评估器异常：{error}")

    if len(lines) == 1:
        lines.append("没有拿到具体建议，请补充可运行代码、真实数字和一个明确的取舍。")

    lines.append("保留已经正确的结论和能跑的代码，只修改上面指出的问题，输出完整 JSON。")
    return "\n".join(lines)


def evaluate_safely(llm: LLMClient, markdown: str, settings: Settings) -> Evaluation:
    try:
        return evaluate(llm, markdown, settings)
    except (LLMError, ValidationError, ValueError) as exc:
        logger.warning("评估器失败，按未达标处理：%s", exc)
        return fallback_evaluation(f"评估器调用失败：{exc}")


__all__ = [
    "EVALUATOR_PROMPT_VERSION",
    "TEMPLATE_VERSION",
    "build_feedback",
    "evaluate",
    "evaluate_safely",
    "fallback_evaluation",
]
