from __future__ import annotations

import sqlite3
import threading
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.adapters.feishu import get_feishu_client
from app.agents.llm import build_llm
from app.agents.schemas import TEMPLATE_VERSION
from app.config import Settings, get_settings
from app.graph.note_ingest import (
    ProgressHook,
    ReviewHook,
    build_ingest_graph,
    publish_draft,
)
from app.store.db import add_event, create_draft, get_draft, init_db, set_status

Graph = Any

_LOCK = threading.Lock()
# 本地单用户工具：同一时刻只跑一个图，避免多线程同时写同一个 SQLite checkpoint
_RUN_LOCK = threading.Lock()
_GRAPHS: dict[str, Graph] = {}
_CONNS: dict[str, sqlite3.Connection] = {}


def _graph_key(settings: Settings) -> str:
    return "|".join(
        [
            str(settings.graph_db_path.resolve()),
            settings.llm_model,
            str(settings.pass_score),
            str(settings.max_rewrites),
        ]
    )


def get_graph(
    settings: Settings | None = None,
    review_hook: ReviewHook | None = None,
    progress_hook: ProgressHook | None = None,
) -> Graph:
    settings = settings or get_settings()
    key = _graph_key(settings)

    with _LOCK:
        conn = _CONNS.get(key)
        if conn is None:
            settings.ensure_dirs()
            init_db(settings.db_path)
            conn = sqlite3.connect(str(settings.graph_db_path), check_same_thread=False)
            _CONNS[key] = conn

        if review_hook is not None or progress_hook is not None:
            # 带 hook 的实例不缓存：hook 通常是这次调用的闭包（比如往终端打印）
            return build_ingest_graph(
                settings=settings,
                llm=build_llm(settings),
                feishu=get_feishu_client(settings),
                checkpointer=SqliteSaver(conn),
                review_hook=review_hook,
                progress_hook=progress_hook,
            )

        if key not in _GRAPHS:
            _GRAPHS[key] = build_ingest_graph(
                settings=settings,
                llm=build_llm(settings),
                feishu=get_feishu_client(settings),
                checkpointer=SqliteSaver(conn),
            )
        return _GRAPHS[key]


def reset_graphs() -> None:
    """测试里换 data_dir 后调用，顺便关掉 checkpoint 连接。"""
    with _LOCK:
        for conn in _CONNS.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _CONNS.clear()
        _GRAPHS.clear()


def _config(draft_id: str) -> dict[str, Any]:
    # 自动返工最多 3 轮，每轮 4 个节点，默认递归上限 25 会正好卡住
    return {"configurable": {"thread_id": draft_id}, "recursion_limit": 50}


def start_note(
    point: str,
    *,
    settings: Settings | None = None,
    review_hook: ReviewHook | None = None,
    progress_hook: ProgressHook | None = None,
    draft_id: str | None = None,
) -> dict[str, Any]:
    """建草稿并一路跑到审阅挂起，返回最新的草稿行。"""
    settings = settings or get_settings()
    point = (point or "").strip()
    if not point:
        raise ValueError("知识点不能为空")

    graph = get_graph(settings, review_hook, progress_hook)
    draft = create_draft(
        settings.db_path,
        raw_point=point,
        template_version=TEMPLATE_VERSION,
        draft_id=draft_id,
    )
    try:
        with _RUN_LOCK:
            graph.invoke(
                {"draft_id": draft["draft_id"], "raw_point": point, "rewrite_count": 0},
                _config(draft["draft_id"]),
            )
    except Exception:
        # 没跑到审阅就失败（模型报错、校验连续失败）：标成 discarded，
        # 免得列表里留一条点不动的「待审阅」
        set_status(
            settings.db_path, draft["draft_id"], "discarded", expected="awaiting_review"
        )
        raise
    return get_draft(settings.db_path, draft["draft_id"]) or draft


def resume_note(
    draft_id: str,
    action: str,
    *,
    note: str | None = None,
    settings: Settings | None = None,
    review_hook: ReviewHook | None = None,
) -> dict[str, Any]:
    """审阅决定：approve 写入飞书，reject 丢弃，rewrite 重新生成一次。"""
    if action not in ("approve", "reject", "rewrite"):
        raise ValueError(f"未知审阅动作: {action}")

    settings = settings or get_settings()
    draft = get_draft(settings.db_path, draft_id)
    if draft is None:
        raise LookupError(f"草稿不存在: {draft_id}")
    if draft["status"] != "awaiting_review":
        raise RuntimeError(f"草稿状态是 {draft['status']}，不在待审阅状态")
    if action == "rewrite" and int(draft["rewrite_count"]) >= settings.max_rewrites:
        raise RuntimeError(f"重写次数已用完（{settings.max_rewrites} 次）")

    graph = get_graph(settings, review_hook)

    # 先落终态再 resume：万一 resume 抛错，DB 状态也和用户的决定一致
    if action == "reject":
        if not set_status(
            settings.db_path, draft_id, "rejected", expected="awaiting_review"
        ):
            raise RuntimeError("状态已被其他操作修改，请刷新后重试")

    with _RUN_LOCK:
        graph.invoke(
            Command(resume={"action": action, "note": note or ""}),
            _config(draft_id),
        )
    return get_draft(settings.db_path, draft_id) or draft


def retry_write(
    draft_id: str, *, settings: Settings | None = None
) -> dict[str, Any]:
    """导入失败后重试写入：只重跑「建文档 + 快照」这一段，不重新生成。

    草稿在 write_failed 时会回滚到 approved，正好是 publish_draft 的入口状态。
    """
    settings = settings or get_settings()
    draft = get_draft(settings.db_path, draft_id)
    if draft is None:
        raise LookupError(f"草稿不存在: {draft_id}")
    if draft["status"] != "approved":
        raise RuntimeError(
            f"草稿状态是 {draft['status']}，只有 approved（写入失败回滚）才能重试"
        )

    result = publish_draft(
        settings=settings, feishu=get_feishu_client(settings), draft_id=draft_id
    )
    add_event(
        settings.db_path,
        draft_id=draft_id,
        type_="committed",
        payload={"note_id": result.get("note_id"), "doc_token": result.get("doc_token")},
    )
    return get_draft(settings.db_path, draft_id) or draft


__all__ = ["get_graph", "reset_graphs", "resume_note", "retry_write", "start_note"]
