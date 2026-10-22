from __future__ import annotations

from pydantic import ValidationError

from app.agents.checks import validate_code
from app.agents.llm import LLMClient, LLMError
from app.agents.prompts import EXPAND_SYSTEM, EXPAND_USER
from app.agents.schemas import DraftContent


def generate_draft(
    llm: LLMClient,
    point: str,
    *,
    feedback: str | None = None,
    max_retries: int = 2,
) -> DraftContent:
    user = EXPAND_USER.format(point=point)
    if feedback:
        # 自动返工时把上一轮的评估建议带进来，避免模型原地重写一遍
        user = f"{user}\n\n上一版没有通过评审，请按下面的反馈重写：\n{feedback}"
    last_error: str | None = None

    for _ in range(max_retries + 1):
        prompt = user
        if last_error:
            prompt = (
                f"{user}\n\n上一次输出没有通过校验，请修正后重新输出完整 JSON。"
                f"校验错误：{last_error}"
            )
        raw = llm.chat_json(EXPAND_SYSTEM, prompt, temperature=0.7)
        try:
            content = DraftContent.model_validate(raw)
            validate_code(content)
            return content
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:500]

    raise LLMError(f"连续 {max_retries + 1} 次生成未通过校验：{last_error}")
