from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings

from .base import DocRef


class FakeFeishu:
    """把 Markdown 落到本地文件，模拟「飞书里多了一篇文档」。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.dir = settings.data_dir / "fake_feishu"
        self.dir.mkdir(parents=True, exist_ok=True)

    def create_doc_from_markdown(
        self, *, title: str, markdown: str, draft_id: str
    ) -> DocRef:
        token = f"docx_{uuid.uuid4().hex[:12]}"
        path = self.dir / f"{token}.md"
        header = f"<!-- draft:{draft_id} title:{title} -->\n\n"
        path.write_text(header + markdown, encoding="utf-8")
        return DocRef(doc_token=token, url=f"fake://{token}", local_path=str(path))

    def revoke(self, doc_token: str) -> None:
        path: Path = self.dir / f"{doc_token}.md"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # 挂载目录可能不允许删除，忽略即可；真实实现会调云文档删除接口
            pass

    def read_markdown(self, doc_token: str) -> str:
        path = self.dir / f"{doc_token}.md"
        if not path.exists():
            raise FileNotFoundError(f"fake 文档不存在：{doc_token}")
        text = path.read_text(encoding="utf-8")
        # 去掉写入时加的注释头，模拟从飞书读回来的正文
        if text.startswith("<!-- draft:"):
            _, _, rest = text.partition("\n\n")
            return rest
        return text

    def send_text(self, *, chat_id: str, text: str) -> str:
        message_id = f"om_fake_{uuid.uuid4().hex[:10]}"
        self._append(
            "messages.log",
            json.dumps(
                {"message_id": message_id, "chat_id": chat_id, "type": "text", "text": text},
                ensure_ascii=False,
            )
            + "\n",
        )
        return message_id

    def send_review_card(
        self, *, chat_id: str, payload: dict[str, Any], review_url: str | None = None
    ) -> str:
        from .cards import build_review_card

        message_id = f"om_fake_{uuid.uuid4().hex[:10]}"
        record = {
            "message_id": message_id,
            "chat_id": chat_id,
            "review_url": review_url,
            "card": build_review_card(payload, review_url=review_url),
        }
        self._append(
            "cards.jsonl", json.dumps(record, ensure_ascii=False) + "\n"
        )
        return message_id

    def _append(self, name: str, text: str) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as handle:
            handle.write(text)
