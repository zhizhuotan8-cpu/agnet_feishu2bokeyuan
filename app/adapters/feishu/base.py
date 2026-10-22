from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DocRef:
    doc_token: str
    url: str | None = None
    local_path: str | None = None
    # 非致命问题（比如加协作者失败：文档建好了但你还看不到）
    warning: str | None = None


class FeishuClient(Protocol):
    """写入飞书的最小接口。fake / live 两个实现，上层不用改。"""

    def create_doc_from_markdown(
        self, *, title: str, markdown: str, draft_id: str
    ) -> DocRef: ...

    def revoke(self, doc_token: str) -> None: ...

    def read_markdown(self, doc_token: str) -> str: ...

    def send_text(self, *, chat_id: str, text: str) -> str: ...

    def send_review_card(
        self, *, chat_id: str, payload: dict[str, Any], review_url: str | None = None
    ) -> str: ...

    def update_card(self, *, message_id: str, card: dict[str, Any]) -> None: ...
