"""飞书卡片回调监听：WebSocket 长连接，不需要公网回调地址。

只订阅 card.action.trigger（新版），别同时开 trigger_v1，否则一次点击收两份。
点击后必须 3 秒内响应，所以这里只做参数校验 + 起线程，真正的 resume 在后台跑。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.config import Settings
from app.graph.runner import resume_note
from app.store.db import get_draft, get_note_by_draft

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"approve", "rewrite", "reject"}


def _value(data: Any) -> dict[str, Any]:
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    value = getattr(action, "value", None)
    return value if isinstance(value, dict) else {}


def _chat_id(data: Any, fallback: str) -> str:
    event = getattr(data, "event", None)
    context = getattr(event, "context", None)
    return str(getattr(context, "open_chat_id", "") or fallback)


def _toast_response(text: str, kind: str = "success") -> Any:
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    # 官方示例是属性赋值；个别版本也接受 dict，失败时退回去
    try:
        toast = CallBackToast()
        toast.type = kind
        toast.content = text
    except (TypeError, AttributeError):
        toast = CallBackToast({"type": kind, "content": text})
    response = P2CardActionTriggerResponse()
    response.toast = toast
    return response


def _handle_action(
    settings: Settings, feishu: Any, draft_id: str, action: str, chat_id: str
) -> None:
    try:
        draft = resume_note(draft_id, action, settings=settings)
        if action == "approve":
            note = get_note_by_draft(settings.db_path, draft_id) or {}
            url = note.get("doc_url")
            text = f"已通过并写入飞书：{url}" if url else "已通过"
        elif action == "rewrite":
            text = f"{draft_id} 已重写，新卡片稍后发到群里"
        else:
            text = f"{draft_id} 已丢弃"
        logger.info("卡片操作完成：%s %s -> %s", draft_id, action, draft["status"])
    except Exception as exc:  # noqa: BLE001 - 后台线程里必须自己兜住
        logger.warning("卡片操作失败：%s %s -> %s", draft_id, action, exc)
        text = f"{draft_id} 操作失败：{exc}"

    try:
        feishu.send_text(chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001 - 通知失败不影响主流程
        logger.exception("回发结果消息失败")


def build_handler(settings: Settings, feishu: Any) -> Any:
    import lark_oapi as lark

    def on_card_action(data: Any) -> Any:
        value = _value(data)
        action = str(value.get("action") or "")
        draft_id = str(value.get("draft_id") or "")
        chat_id = _chat_id(data, settings.feishu_chat_id)

        if action not in VALID_ACTIONS or not draft_id:
            return _toast_response("卡片参数不完整，忽略这次点击", "error")

        draft = get_draft(settings.db_path, draft_id)
        if draft is None:
            return _toast_response("草稿不存在（可能是旧卡片）", "error")
        if draft["status"] != "awaiting_review":
            return _toast_response("这张卡片已经处理过了", "warning")

        threading.Thread(
            target=_handle_action,
            args=(settings, feishu, draft_id, action, chat_id),
            daemon=True,
            name=f"card-{draft_id}",
        ).start()
        return _toast_response("已提交，处理结果会发到群里")

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )


def run_listener(settings: Settings, *, log_level: str = "INFO") -> None:
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - 依赖没装时给出可执行的提示
        raise RuntimeError(
            "卡片回调需要 lark-oapi>=1.6.4：pip install 'lark-oapi>=1.6.4'"
        ) from exc

    # 没有 basicConfig 的话，下面的版本警告只进 lastResort，终端看不到
    logging.basicConfig(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1.5.3 会静默丢掉 CARD 帧（200340），版本不对时先报警再连
    from importlib.metadata import PackageNotFoundError, version

    try:
        sdk_version = version("lark-oapi")
    except PackageNotFoundError:  # pragma: no cover - 源码方式安装时没有元数据
        sdk_version = ""
    if sdk_version:
        logger.info("lark-oapi %s", sdk_version)
        head = sdk_version.split(".", 2)[:2]
        try:
            major, minor = int(head[0]), int(head[1])
        except (ValueError, IndexError):
            major = minor = 0
        if (major, minor) < (1, 6):
            logger.warning(
                "lark-oapi %s 低于 1.6，卡片回调可能被静默丢弃（点按钮没反应）；"
                "请 pip install -U 'lark-oapi>=1.6.4'",
                sdk_version,
            )

    from app.adapters.feishu import get_feishu_client

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 没配，没法建立长连接")

    feishu = get_feishu_client(settings)
    handler = build_handler(settings, feishu)
    level = getattr(lark.LogLevel, str(log_level).upper(), lark.LogLevel.INFO)
    client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=level,
    )
    logger.info("飞书长连接已启动，等待卡片回调（Ctrl+C 退出）")
    client.start()
