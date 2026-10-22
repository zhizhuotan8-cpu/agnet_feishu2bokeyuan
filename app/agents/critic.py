from __future__ import annotations

import re

from app.agents.llm import LLMClient, LLMError
from app.agents.prompts import CRITIC_SYSTEM, CRITIC_USER
from app.agents.schemas import DraftContent

# 扩写和自检会给出同一条断言的不同写法（句末标点、空格、括号），
# 比对前先去掉这些噪声，免得同一个待核实点列两遍
_NOISE = re.compile(r"[\s，。；、：:;,.!?！？（）()\[\]【】\"'“”‘’·…\-]+")


def _fingerprint(text: str) -> str:
    return _NOISE.sub("", text).lower()


def critique(
    llm: LLMClient, *, point: str, markdown: str, content: DraftContent
) -> DraftContent:
    """只补充 uncertain_claims，不改正文。自检失败不阻塞主流程。"""
    try:
        raw = llm.chat_json(
            CRITIC_SYSTEM,
            CRITIC_USER.format(point=point, markdown=markdown),
            temperature=0.2,
            max_tokens=600,
        )
    except LLMError:
        return content

    extra = raw.get("uncertain_claims")
    if not isinstance(extra, list):
        return content

    return content.model_copy(update={"uncertain_claims": _merge(content.uncertain_claims, extra)})


# 一条断言只是另一条的半句时（多了括号补充、后半句），留信息更全的那条。
# 长度差太悬殊的不合并，避免「GIL 不存在」被并进「…不存在于 Jython」这类不同断言
_SUBSET_RATIO = 0.6


def _merge(base: list[str], extra: list[str]) -> list[str]:
    merged: list[str] = []
    keys: list[str] = []
    # base 自己也过一遍，扩写自己就可能给出半句重复；extra 再往里并
    for item in [*base, *extra]:
        text = str(item).strip()
        key = _fingerprint(text)
        if not text or not key:
            continue
        if key in keys:
            continue
        host = next(
            (
                i
                for i, k in enumerate(keys)
                if (k in key or key in k)
                and min(len(k), len(key)) / max(len(k), len(key)) >= _SUBSET_RATIO
            ),
            None,
        )
        if host is None:
            merged.append(text)
            keys.append(key)
        elif len(key) > len(keys[host]):
            merged[host] = text
            keys[host] = key
    return merged
