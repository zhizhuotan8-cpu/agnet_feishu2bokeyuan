from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TEMPLATE_VERSION = "blog_v1"
# 评分提示词的版本在 app/agents/prompts.py 的 EVALUATOR_PROMPT_VERSION，别在这里再定义一个

Dimension = Literal["accuracy", "specificity", "density", "structure", "readability"]


class Pitfall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=2, max_length=30, description="现象，不写“要注意”")
    detail: str = Field(min_length=4, description="触发条件，1-2 句")


class Reference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    url: str | None = None


class DraftContent(BaseModel):
    """模型唯一的输出结构，字段与 blog_post.md.j2 一一对应。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=2, max_length=40)
    hook: str = Field(min_length=5, description="1-2 句导语")
    what: str = Field(min_length=10, description="100 字内给结论")
    why: list[str] = Field(min_length=1, description="2-4 段，讲原理与取舍")
    example_lang: str = Field(default="python")
    example_code: str = Field(min_length=1, description="10-30 行，语法完整")
    example_note: str = Field(min_length=2)
    pitfalls: list[Pitfall] = Field(min_length=1)
    summary: str = Field(min_length=5)
    tags: list[str] = Field(default_factory=list)
    refs: list[Reference] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    uncertain_claims: list[str] = Field(default_factory=list)


class Deduction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dimension: Dimension
    points: float = Field(ge=0, le=10)
    quote: str = Field(min_length=2, description="必须能在原文中原样找到")
    reason: str = Field(min_length=2)
    quote_verified: bool = True


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 模型偶尔会给出 8.5 这种小数，这里放宽成 float，由 scoring.clamp_score 归一
    scores: dict[str, float]
    deductions: list[Deduction] = Field(default_factory=list)
    # 给下一轮重写用的具体修改建议；评估失败时为空
    suggestions: list[str] = Field(default_factory=list)
    veto: str | None = None
    # 评估器自身没跑通（网络、空返回、JSON 不合规）时填这里，
    # 和模型给出的「一票否决」分开，免得页面上把故障说成内容问题
    error: str | None = None
    total: float = 0.0
    verdict: Literal["pass", "below_bar"] = "below_bar"
