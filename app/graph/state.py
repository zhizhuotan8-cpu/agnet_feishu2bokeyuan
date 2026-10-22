from __future__ import annotations

from typing import Any, TypedDict


class NoteState(TypedDict, total=False):
    """链路 A 在节点之间传递的状态。

    content / evaluation 存 dict 而不是 pydantic 对象：SqliteSaver 会把它
    序列化成 JSON 存 checkpoint，纯 dict 最稳，读出来再用 model_validate 还原。
    """

    draft_id: str
    raw_point: str
    content: dict[str, Any] | None
    markdown: str | None
    evaluation: dict[str, Any] | None
    score: float | None
    verdict: str | None

    review_action: str | None  # 审阅动作：approve 通过 / reject 拒绝 / rewrite 重写
    review_note: str | None
    review_payload: dict[str, Any] | None
    rewrite_count: int
    # 上一轮评估整理的改稿说明；自动返工和手动重写都会带进扩写 prompt
    feedback: str | None

    note_id: str | None
    doc_token: str | None
    doc_url: str | None
    local_path: str | None

    error: str | None
