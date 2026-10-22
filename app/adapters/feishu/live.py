"""真实飞书接入：tenant_access_token、导入任务、协作者、消息、文档回读。

设计上只有这一层碰 HTTP；上层图谱、CLI 拿到的都是普通返回值或 FeishuError。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

import httpx

from app.config import Settings

from .base import DocRef
from .cards import build_review_card
from .docx_md import blocks_to_markdown

logger = logging.getLogger(__name__)

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_PATH = "/open-apis/drive/v1/medias/upload_all"
IMPORT_PATH = "/open-apis/drive/v1/import_tasks"
FILE_DELETE_PATH = "/open-apis/drive/v1/files/{token}"
PERMISSION_PATH = "/open-apis/drive/v1/permissions/{token}/members"
MESSAGE_PATH = "/open-apis/im/v1/messages"
CHATS_PATH = "/open-apis/im/v1/chats"
CHAT_MEMBERS_PATH = "/open-apis/im/v1/chats/{chat_id}/members"
BOT_INFO_PATH = "/open-apis/bot/v3/info"
BLOCKS_PATH = "/open-apis/docx/v1/documents/{token}/blocks"
RAW_CONTENT_PATH = "/open-apis/docx/v1/documents/{token}/raw_content"

# 文档 token 只允许安全字符，避免拼路径时被注入
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')

RATE_LIMIT_CODE = 99991400
TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}

# 导入任务的状态：0 成功，1/2 还在跑，其余都是终态失败
IMPORT_PENDING = {1, 2}


class FeishuError(RuntimeError):
    """飞书返回了非 0 的 code，或者响应结构不对。"""

    def __init__(self, message: str, *, code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class FeishuAuthError(FeishuError):
    """凭证缺失、app_id/secret 错、token 拿不到：重试没用。"""


class FeishuRateLimited(FeishuError):
    """频控，重试几次仍然失败。"""


class FeishuImportError(FeishuError):
    """导入任务失败或超时。ticket 可以拿来续查，别重新提交。"""


def _safe_filename(title: str, limit: int = 80) -> str:
    name = _UNSAFE_FILENAME.sub(" ", title or "").strip()
    return (name[:limit] or "未命名笔记")


def _check_token(token: str) -> str:
    if not token or not _SAFE_TOKEN.match(token):
        raise FeishuError(f"文档 token 不合法：{token!r}")
    return token


class LiveFeishu:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not settings.feishu_app_id or not settings.feishu_app_secret:
            raise FeishuAuthError(
                "FEISHU_MODE=live 但没配 FEISHU_APP_ID / FEISHU_APP_SECRET，"
                "去 .env 里填上（见 .env.example）"
            )
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._client = client or httpx.Client(
            base_url=settings.feishu_base_url,
            timeout=settings.request_timeout,
        )
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._token = ""
        self._token_expire_at = 0.0

    # ---------- 基础设施 ----------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _tenant_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._token_expire_at:
                return self._token

            try:
                resp = self._client.post(
                    TOKEN_PATH,
                    json={
                        "app_id": self.settings.feishu_app_id,
                        "app_secret": self.settings.feishu_app_secret,
                    },
                )
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FeishuAuthError(f"获取 tenant_access_token 失败：{exc}") from exc

            if data.get("code") != 0:
                raise FeishuAuthError(
                    f"获取 tenant_access_token 失败：{data.get('code')} {data.get('msg')}",
                    code=data.get("code"),
                )
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise FeishuAuthError("鉴权响应里没有 tenant_access_token")

            # 提前 5 分钟过期，避免正好在请求途中失效
            expire = int(data.get("expire") or 7200)
            self._token = token
            self._token_expire_at = now + max(expire - 300, 60)
            return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retries: int = 2,
        auth: bool = True,
    ) -> dict[str, Any]:
        refresh_token = False
        for attempt in range(retries + 1):
            headers = {"Authorization": f"Bearer {self._tenant_token()}"} if auth else {}
            try:
                resp = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    files=files,
                    data=data,
                    headers=headers,
                )
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt < retries:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                raise FeishuError(f"请求飞书失败（{method} {path}）：{exc}") from exc

            code = payload.get("code")
            if code == 0:
                # 多数接口包一层 data；/bot/v3/info 之类直接把字段放顶层
                body = payload.get("data")
                return body if isinstance(body, dict) else payload

            if code in TOKEN_INVALID_CODES and not refresh_token:
                refresh_token = True
                with self._lock:
                    self._token = ""
                continue

            if code == RATE_LIMIT_CODE:
                if attempt < retries:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                raise FeishuRateLimited(
                    f"飞书接口限流（{path}），稍后再试", code=code, detail=payload
                )

            raise FeishuError(
                f"飞书接口 {path} 返回 {code}：{payload.get('msg')}",
                code=code,
                detail=payload,
            )
        raise FeishuError(f"请求飞书失败（{method} {path}）：重试次数用尽")

    # ---------- 导入文档 ----------

    def _upload_markdown(self, title: str, markdown: str) -> str:
        raw = markdown.encode("utf-8")
        filename = f"{_safe_filename(title)}.md"
        # 导入用的临时素材：parent_type 固定 ccm_import_open，extra 告诉飞书按 docx/md 解析
        data = {
            "file_name": filename,
            "parent_type": "ccm_import_open",
            "parent_node": "",
            "size": str(len(raw)),
            "extra": json.dumps(
                {"obj_type": "docx", "file_extension": "md"}, ensure_ascii=False
            ),
        }
        files = {"file": (filename, raw, "text/markdown")}
        result = self._request("POST", UPLOAD_PATH, files=files, data=data)
        token = str(result.get("file_token") or "")
        if not token:
            raise FeishuError("上传 Markdown 成功但没返回 file_token")
        return token

    def _create_import(self, file_token: str, title: str) -> str:
        # mount_key 是必填字段：空字符串表示云空间根目录，不能整个省略
        point = {
            "mount_type": 1,
            "mount_key": self.settings.feishu_folder_token or "",
        }
        body = {
            "file_extension": "md",
            "file_token": file_token,
            "type": "docx",
            "file_name": _safe_filename(title),
            "point": point,
        }
        result = self._request("POST", IMPORT_PATH, json_body=body)
        ticket = str(result.get("ticket") or "")
        if not ticket:
            raise FeishuImportError("创建导入任务成功但没返回 ticket")
        return ticket

    def _wait_import(
        self, ticket: str, *, timeout: float = 90.0, interval: float = 1.0
    ) -> tuple[str, str | None]:
        deadline = self._clock() + timeout
        path = f"{IMPORT_PATH}/{ticket}"
        while True:
            data = self._request("GET", path)
            result = data.get("result") or {}
            status = result.get("job_status")
            if status == 0:
                token = str(result.get("token") or "")
                if not token:
                    raise FeishuImportError("导入成功但没返回文档 token", detail=result)
                return token, (str(result.get("url")) if result.get("url") else None)
            if status not in IMPORT_PENDING:
                raise FeishuImportError(
                    f"导入失败（job_status={status}）：{result.get('job_error_msg') or '未知原因'}",
                    code=status if isinstance(status, int) else None,
                    detail=result,
                )
            if self._clock() >= deadline:
                raise FeishuImportError(
                    f"导入超过 {timeout:.0f} 秒还没完成（ticket={ticket}）。"
                    "稍后可以用同一 ticket 再查，不要重新提交，否则会建出重复文档",
                    detail=result,
                )
            self._sleep(interval)

    def add_collaborator(self, doc_token: str, open_id: str) -> None:
        body = {
            "member_type": "openid",
            "member_id": open_id,
            "perm": "full_access",
            "need_notification": True,
        }
        self._request(
            "POST",
            PERMISSION_PATH.format(token=_check_token(doc_token)),
            params={"type": "docx"},
            json_body=body,
        )

    def create_doc_from_markdown(
        self, *, title: str, markdown: str, draft_id: str
    ) -> DocRef:
        file_token = self._upload_markdown(title, markdown)
        ticket = self._create_import(file_token, title)
        doc_token, url = self._wait_import(ticket)

        warning = None
        owner = self.settings.feishu_owner_open_id.strip()
        if owner:
            try:
                self.add_collaborator(doc_token, owner)
            except FeishuError as exc:
                # 文档已经建好了，加协作者失败不该让整条链路失败，但必须显式提示
                warning = f"文档已创建，但加协作者失败（你可能看不到它）：{exc}"
                logger.warning(warning)
        return DocRef(doc_token=doc_token, url=url, warning=warning)

    def revoke(self, doc_token: str) -> None:
        self._request(
            "DELETE",
            FILE_DELETE_PATH.format(token=_check_token(doc_token)),
            params={"type": "docx"},
        )

    # ---------- 读回文档 ----------

    def read_markdown(self, doc_token: str) -> str:
        token = _check_token(doc_token)
        try:
            items: list[dict[str, Any]] = []
            page_token = ""
            while True:
                params: dict[str, Any] = {
                    "page_size": 500,
                    "document_revision_id": -1,
                }
                if page_token:
                    params["page_token"] = page_token
                data = self._request(
                    "GET", BLOCKS_PATH.format(token=token), params=params
                )
                items.extend(item for item in (data.get("items") or []) if isinstance(item, dict))
                if not data.get("has_more"):
                    break
                page_token = str(data.get("page_token") or "")
                if not page_token:
                    break
            return blocks_to_markdown(items)
        except FeishuError as exc:
            # 块接口挂了至少还能拿到纯文本，发布计划里会标「降级」
            logger.warning("块接口读取失败，降级到 raw_content：%s", exc)
            data = self._request("GET", RAW_CONTENT_PATH.format(token=token))
            return str(data.get("content") or "")

    # ---------- 消息 ----------

    def _send_message(self, *, chat_id: str, msg_type: str, content: dict[str, Any]) -> str:
        body = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        result = self._request(
            "POST",
            MESSAGE_PATH,
            params={"receive_id_type": "chat_id"},
            json_body=body,
        )
        return str(result.get("message_id") or "")

    def send_text(self, *, chat_id: str, text: str) -> str:
        return self._send_message(chat_id=chat_id, msg_type="text", content={"text": text})

    def send_review_card(
        self, *, chat_id: str, payload: dict[str, Any], review_url: str | None = None
    ) -> str:
        card = build_review_card(payload, review_url=review_url)
        return self._send_message(chat_id=chat_id, msg_type="interactive", content=card)

    # ---------- 自检 ----------

    def bot_info(self) -> dict[str, Any]:
        return self._request("GET", BOT_INFO_PATH)

    def list_chats(self, page_size: int = 50) -> list[dict[str, Any]]:
        data = self._request("GET", CHATS_PATH, params={"page_size": page_size})
        return [item for item in (data.get("items") or []) if isinstance(item, dict)]

    def list_chat_members(
        self, chat_id: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """群成员（不含机器人）。用 open_id 作为 member_id，方便找自己的 open_id。"""
        path = CHAT_MEMBERS_PATH.format(chat_id=_check_token(chat_id))
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "member_id_type": "open_id",
                "page_size": page_size,
            }
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", path, params=params)
            items.extend(
                item for item in (data.get("items") or []) if isinstance(item, dict)
            )
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
        return items
