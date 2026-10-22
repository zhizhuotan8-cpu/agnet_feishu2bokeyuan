from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

import httpx

from app.config import Settings


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    """key 错、模型名错这类重试也没用的问题。"""


# 推理型模型（deepseek-reasoner 之类）的思维链也吃 max_tokens，
# 预算不够时正文会是空的、finish_reason=length。这里留出翻倍重试的上限
MAX_TOKEN_CEILING = 8000


class LLMClient(Protocol):
    model: str

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2400,
    ) -> dict[str, Any]: ...


def _extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"模型没有返回 JSON：{text[:200]}")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 不是对象")
    return data


class OpenAICompatibleClient:
    """任何 OpenAI 兼容的 /chat/completions 服务都能用。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.llm_model

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2400,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        using_json_mode = json_mode
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if using_json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(3):
            # 有些 OpenAI 兼容服务在 json_object 模式下会返回空串，第二次起退回普通文本，
            # 再靠 _extract_json 从围栏/前后缀里抠 JSON
            if attempt == 1 and using_json_mode:
                using_json_mode = False
                payload.pop("response_format", None)
            try:
                with httpx.Client(timeout=self.settings.request_timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"服务端 {resp.status_code}：{resp.text[:200]}")
                if resp.status_code >= 400:
                    # 个别服务支持对话但不支持 response_format，这里降级而不是当配置错误
                    if (
                        resp.status_code == 400
                        and using_json_mode
                        and "response_format" in resp.text
                    ):
                        using_json_mode = False
                        payload.pop("response_format", None)
                        raise LLMError(f"服务端不支持 json_object：{resp.text[:200]}")
                    raise LLMConfigError(
                        f"HTTP {resp.status_code}（不重试）：{resp.text[:300]}"
                    )

                choice = resp.json()["choices"][0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                if not content.strip():
                    finish = choice.get("finish_reason")
                    reasoning = str(message.get("reasoning_content") or "")
                    if finish == "length" and max_tokens < MAX_TOKEN_CEILING:
                        max_tokens = min(max_tokens * 2, MAX_TOKEN_CEILING)
                        payload["max_tokens"] = max_tokens
                        raise LLMError(
                            "推理过程占满了输出预算"
                            f"（finish_reason=length，reasoning_content {len(reasoning)} 字符），"
                            f"把 max_tokens 提到 {max_tokens} 重试"
                        )
                    if reasoning:
                        raise LLMError(
                            f"模型只返回了推理过程（{len(reasoning)} 字符），正文为空；"
                            f"finish_reason={finish}，max_tokens 已到 {max_tokens}"
                        )
                    raise LLMError(f"模型返回空内容（finish_reason={finish}）")
                return _extract_json(content)
            except LLMConfigError:
                raise
            except (httpx.HTTPError, KeyError, IndexError, TypeError, LLMError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"调用模型失败：{last_error}")


class FakeLLM:
    """无凭证也能跑通整条链路的确定性替身。"""

    model = "fake"

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2400,
    ) -> dict[str, Any]:
        if "TASK:EVALUATE" in system:
            return _fake_evaluation(user)
        if "TASK:CRITIQUE" in system:
            return {"uncertain_claims": ["fake 模式没有真正核对事实，正文仅供流程验证"]}
        return _fake_draft(_extract_point(user))


def _extract_point(user: str) -> str:
    for line in user.splitlines():
        if line.startswith("知识点："):
            return line.split("：", 1)[1].strip()
    return user.strip()[:50] or "未命名知识点"


def _fake_draft(point: str) -> dict[str, Any]:
    return {
        "title": f"{point[:24]}：一次讲清楚",
        "hook": f"「{point}」这个词你大概率见过，但真要说清它解决什么问题，很多人会卡住。",
        "what": (
            f"先给结论：{point} 不是孤立的名词，它是一组取舍的结果。"
            "这段正文由 fake 模式生成，只用来验证「扩写 → 评分 → 审阅 → 写入」的链路是否通畅。"
        ),
        "why": [
            "任何设计都有它要解决的问题。先看问题是什么，再看它付出了什么代价，最后才知道什么时候不该用它。",
            "fake 模式没有调用真实模型，所以这里不会给你真正的技术结论。配置好 LLM_API_KEY 后重跑，这一段才会被真实内容替换。",
        ],
        "example_lang": "python",
        "example_code": (
            "def demo():\n"
            '    """fake 模式示例，未调用真实模型。"""\n'
            '    return "replace me with real content"\n\n'
            'print(demo())\n'
        ),
        "example_note": "这段代码只保证语法完整，用来验证代码块渲染和语法检查。",
        "pitfalls": [
            {"title": "把示例当结论", "detail": "fake 模式不会真的解释知识点，别拿它当笔记。"},
            {"title": "忘记配密钥", "detail": "只填 LLM_API_KEY 还不够，base_url 和模型名也要匹配。"},
        ],
        "summary": "先用 fake 跑通流程，再换成真实模型看内容质量。",
        "tags": [point[:8] or "示例"],
        "refs": [],
        "confidence": "low",
        "uncertain_claims": ["当前为 fake 模式，正文仅用于验证流程"],
    }


def _fake_evaluation(user: str) -> dict[str, Any]:
    first_line = next(
        (line.strip("# ").strip() for line in user.splitlines() if line.strip().startswith("#")),
        "正文",
    )
    return {
        "scores": {
            "accuracy": 8,
            "specificity": 7,
            "density": 8,
            "structure": 9,
            "readability": 8,
        },
        "deductions": [
            {
                "dimension": "specificity",
                "points": 1.0,
                "quote": first_line,
                "reason": "fake 模式没有给出真实可验证的例子，扣具体性分。",
            }
        ],
        "suggestions": [
            "补一个能直接跑的例子，带上真实输出",
            "把结论段压到三句以内",
        ],
        "veto": None,
    }


def build_llm(settings: Settings) -> LLMClient:
    if settings.use_fake_llm:
        return FakeLLM()
    return OpenAICompatibleClient(settings)
