from __future__ import annotations

from typing import Any

from app.agents.critic import critique
from app.agents.llm import FakeLLM, LLMError
from app.agents.prompts import EXPAND_SYSTEM, EXPAND_USER
from app.agents.schemas import DraftContent


class StubLLM:
    model = "stub"

    def __init__(self, claims: list[str] | None = None, raises: bool = False):
        self.claims = claims or []
        self.raises = raises

    def chat_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        if self.raises:
            raise LLMError("boom")
        return {"uncertain_claims": self.claims}


def _content(claims: list[str]) -> DraftContent:
    raw = FakeLLM().chat_json(EXPAND_SYSTEM, EXPAND_USER.format(point="GIL"))
    return DraftContent.model_validate({**raw, "uncertain_claims": claims})


def test_merges_new_claims() -> None:
    content = _content(["已有的点"])
    out = critique(
        StubLLM(["新的点"]), point="GIL", markdown="正文", content=content
    )
    assert out.uncertain_claims == ["已有的点", "新的点"]


def test_dedupes_punctuation_variants() -> None:
    content = _content(["GIL 的切换间隔默认是 5 毫秒。"])
    out = critique(
        StubLLM(["GIL 的切换间隔默认是 5 毫秒", "GIL 的切换间隔默认是 5 毫秒（sys.getswitchinterval() 可查）。"]),
        point="GIL",
        markdown="正文",
        content=content,
    )
    # 第一条只差句末标点；第二条多了括号内容，指纹不同，保留
    assert out.uncertain_claims == [
        "GIL 的切换间隔默认是 5 毫秒。",
        "GIL 的切换间隔默认是 5 毫秒（sys.getswitchinterval() 可查）。",
    ]


def test_merges_half_sentence_variant() -> None:
    # 真实出现过的情况：一条多了后半句，去重后应该只留信息更全的那条
    short = "GIL 的切换间隔默认是 5 毫秒（sys.getswitchinterval() 可查）"
    long = f"{short}，但不同版本可能调整"
    content = _content([short])
    out = critique(StubLLM([long]), point="GIL", markdown="正文", content=content)
    assert out.uncertain_claims == [long]


def test_keeps_unrelated_short_claim() -> None:
    # 长度悬殊的子串不算重复，避免把不同断言合并掉
    content = _content(["GIL 不存在"])
    out = critique(
        StubLLM(["GIL 不存在于 Jython，因为它是 Java 虚拟机上的实现，不需要跨线程互斥"]),
        point="GIL",
        markdown="正文",
        content=content,
    )
    assert len(out.uncertain_claims) == 2


def test_llm_failure_keeps_original() -> None:
    content = _content(["已有的点"])
    out = critique(StubLLM(raises=True), point="GIL", markdown="正文", content=content)
    assert out.uncertain_claims == ["已有的点"]


def test_ignores_non_list_payload() -> None:
    class BadStub(StubLLM):
        def chat_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
            return {"uncertain_claims": "不是数组"}

    content = _content(["已有的点"])
    out = critique(BadStub(), point="GIL", markdown="正文", content=content)
    assert out.uncertain_claims == ["已有的点"]
