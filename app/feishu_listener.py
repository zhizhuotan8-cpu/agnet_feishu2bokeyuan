"""飞书卡片回调监听：WebSocket 长连接，不需要公网回调地址。

只订阅 card.action.trigger（新版），别同时开 trigger_v1，否则一次点击收两份。
点击后必须 3 秒内响应，所以这里只做参数校验 + 起线程，真正的 resume 在后台跑。
处理完把原卡片改写成结果卡片：这样旧卡片上的「通过」不会再被点到，
用户也能直接在卡片上看到文档链接，或者失败原因 + 重试按钮。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any

from app.adapters.feishu.cards import build_result_card
from app.config import Settings
from app.graph.runner import resume_note, retry_write
from app.store.db import add_event, get_draft, get_note_by_draft

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"approve", "rewrite", "reject"}


def _context_value(data: Any, name: str, fallback: str = "") -> str:
    event = getattr(data, "event", None)
    context = getattr(event, "context", None)
    return str(getattr(context, name, "") or fallback)


def _value(data: Any) -> dict[str, Any]:
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    value = getattr(action, "value", None)
    return value if isinstance(value, dict) else {}


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


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_action(draft: dict[str, Any], value: dict[str, Any], action: str) -> str | None:
    """判断这次点击是否还该执行；返回 None 表示可以执行，否则是给用户看的原因。"""
    status = str(draft.get("status") or "")

    if status == "written":
        return "这篇已经写入过飞书了"
    # approved 只可能来自「写入失败回滚」，此时允许点卡片上的重试按钮
    if status == "approved":
        return None if action == "approve" else "这篇正在等待重新写入，只能重试"
    if status != "awaiting_review":
        return f"这张卡片已经处理过了（当前状态：{status}）"

    # 卡片生成后如果又重写过，计数会变；旧卡片不能作用到新版本上
    expected = _as_int(value.get("rewrite_count"))
    current = _as_int(draft.get("rewrite_count")) or 0
    if expected is not None and expected != current:
        return "这张卡片已过期，请用最新的一张"
    return None


def _draft_title(draft: dict[str, Any]) -> str:
    try:
        content = json.loads(draft.get("content_json") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(content.get("title") or "")


def _handle_action(
    settings: Settings,
    feishu: Any,
    *,
    draft_id: str,
    action: str,
    chat_id: str,
    message_id: str = "",
) -> dict[str, Any]:
    """后台执行一次审阅动作，并把结果写回卡片。返回结果 payload，方便测试断言。"""
    status = "error"
    error: str | None = None
    retryable = False

    try:
        draft = get_draft(settings.db_path, draft_id) or {}
        if action == "approve":
            if str(draft.get("status")) == "approved":
                # 上次写入失败后回滚，卡片上的「重试写入」走这里，不重新生成
                result = retry_write(draft_id, settings=settings)
            else:
                result = resume_note(draft_id, "approve", settings=settings)
            status = "written" if result.get("status") == "written" else "write_failed"
        elif action == "rewrite":
            resume_note(draft_id, "rewrite", settings=settings)
            status = "rewritten"
        else:
            resume_note(draft_id, "reject", settings=settings)
            status = "rejected"
    except Exception as exc:  # noqa: BLE001 - 后台线程里必须自己兜住
        error = str(exc)
        logger.warning("卡片操作失败：%s %s -> %s", draft_id, action, exc)
        current = str((get_draft(settings.db_path, draft_id) or {}).get("status") or "")
        if action == "approve" and current == "written":
            # 另一次点击已经写完了，按成功处理，别把结果卡片改成失败
            status, error = "written", None
        elif action == "reject" and current == "rejected":
            status, error = "rejected", None
        elif action == "approve" and current == "approved":
            # 写入失败会回滚到 approved，修好权限后可以直接在卡片上重试
            status, retryable = "write_failed", True
        else:
            status = "error"

    draft = get_draft(settings.db_path, draft_id) or {}
    note = get_note_by_draft(settings.db_path, draft_id) or {}
    if status == "write_failed" and str(draft.get("status")) == "approved":
        retryable = True

    payload = {
        "status": status,
        "action": action,
        "draft_id": draft_id,
        "title": note.get("title") or _draft_title(draft),
        "score": draft.get("score"),
        "doc_url": note.get("doc_url"),
        "error": error,
        "rewrite_count": int(draft.get("rewrite_count") or 0),
        "retryable": retryable,
    }

    add_event(
        settings.db_path,
        draft_id=draft_id,
        type_="card_action",
        payload={
            "action": action,
            "result": status,
            "review_msg_id": message_id,
            "error": error,
        },
    )

    card = build_result_card(payload)
    if message_id:
        try:
            feishu.update_card(message_id=message_id, card=card)
            return payload
        except Exception:  # noqa: BLE001 - 更新失败就退回发一条文本
            logger.warning("更新卡片失败，改为发文本消息", exc_info=True)

    try:
        feishu.send_text(chat_id=chat_id, text=_summary_text(payload))
    except Exception:  # noqa: BLE001 - 通知失败不影响主流程
        logger.exception("回发结果消息失败")
    return payload


def _summary_text(payload: dict[str, Any]) -> str:
    titles = {
        "written": "已写入飞书",
        "rejected": "已丢弃",
        "rewritten": "已提交重写",
        "write_failed": "写入失败",
        "error": "操作失败",
    }
    head = titles.get(str(payload.get("status")), "操作结束")
    lines = [f"{head}：{payload.get('title') or payload.get('draft_id')}"]
    if payload.get("doc_url"):
        lines.append(str(payload["doc_url"]))
    if payload.get("error"):
        lines.append(f"原因：{payload['error']}")
    return "\n".join(lines)


def build_handler(settings: Settings, feishu: Any) -> Any:
    import lark_oapi as lark

    def on_card_action(data: Any) -> Any:
        value = _value(data)
        action = str(value.get("action") or "")
        draft_id = str(value.get("draft_id") or "")
        chat_id = _context_value(data, "open_chat_id", settings.feishu_chat_id)
        message_id = _context_value(data, "open_message_id")

        if action not in VALID_ACTIONS or not draft_id:
            return _toast_response("卡片参数不完整，忽略这次点击", "error")

        draft = get_draft(settings.db_path, draft_id)
        if draft is None:
            return _toast_response("草稿不存在（可能是旧卡片）", "error")
        reason = check_action(draft, value, action)
        if reason:
            return _toast_response(reason, "warning")

        threading.Thread(
            target=_handle_action,
            args=(settings, feishu),
            kwargs={
                "draft_id": draft_id,
                "action": action,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            daemon=True,
            name=f"card-{draft_id}",
        ).start()
        return _toast_response("已提交，处理结果会更新到这张卡片上")

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )


def run_listener(settings: Settings, *, log_level: str = "INFO") -> None:
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - 依赖没装时给出可执行的提示
        # 报错里带上解释器路径：最常见的不是「没装」，而是装到了另一个 venv/全局环境
        raise RuntimeError(
            "卡片回调需要 lark-oapi>=1.6.4，但当前解释器导入失败：\n"
            f"  解释器：{sys.executable}\n"
            f"  报错：{type(exc).__name__}: {exc}\n"
            "在这个解释器所在的虚拟环境里执行：pip install -e \".[dev]\"\n"
            "（或单独装：pip install \"lark-oapi>=1.6.4\"）"
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
