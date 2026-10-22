from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.adapters.feishu.base import FeishuClient
from app.agents.critic import critique
from app.agents.evaluator import EVALUATOR_PROMPT_VERSION, build_feedback, evaluate_safely
from app.agents.expander import generate_draft
from app.agents.llm import LLMClient
from app.agents.prompts import EXPAND_PROMPT_VERSION
from app.agents.renderer import render_blog
from app.agents.schemas import DraftContent
from app.config import Settings
from app.store.content import write_draft_markdown, write_note_snapshot
from app.store.db import (
    add_event,
    create_note,
    get_draft,
    increment_rewrite,
    save_evaluation,
    set_status,
    update_draft,
)

from .state import NoteState

logger = logging.getLogger(__name__)

ReviewHook = Callable[[dict[str, Any]], None]
ProgressHook = Callable[[dict[str, Any]], None]


class PublishSkipped(RuntimeError):
    """草稿不在 approved 状态，不能写入（通常是重复点击或已经写过了）。"""

    def __init__(self, status: str | None):
        super().__init__(f"草稿状态是 {status}，跳过写入（可能是重复点击）")
        self.status = status


def publish_draft(
    *, settings: Settings, feishu: FeishuClient, draft_id: str
) -> dict[str, Any]:
    """把 approved 的草稿写入飞书：条件改状态 → 建文档 → 快照 → 记笔记 → 改 written。

    单独抽出来，是为了让 CLI 的 `retry-write` 能在导入失败后重试，
    不必重新走一遍生成和评分。
    """
    db = settings.db_path
    draft = get_draft(db, draft_id) or {}
    # 先解析再改状态：内容坏掉时不该把草稿卡在 creating_doc
    content = DraftContent.model_validate_json(draft.get("content_json") or "{}")
    # 以库里最新的正文为准：审阅页和 note-agent show 读的都是它，
    # 用 checkpoint 里的旧快照会把重新渲染过的稿子发成旧版本
    markdown = draft.get("markdown") or render_blog(content)

    if not set_status(db, draft_id, "creating_doc", expected="approved"):
        raise PublishSkipped(draft.get("status"))
    note_id = f"n_{uuid.uuid4().hex[:10]}"

    try:
        ref = feishu.create_doc_from_markdown(
            title=content.title, markdown=markdown, draft_id=draft_id
        )
    except Exception as exc:  # noqa: BLE001 - 失败要能回滚状态再报上去
        set_status(db, draft_id, "approved", expected="creating_doc")
        add_event(
            db, draft_id=draft_id, type_="write_failed", payload={"error": str(exc)}
        )
        raise

    if ref.warning:
        add_event(
            db,
            draft_id=draft_id,
            type_="doc_warning",
            payload={"warning": ref.warning, "doc_token": ref.doc_token},
        )

    path, digest = write_note_snapshot(settings, note_id, markdown)
    create_note(
        db,
        note_id=note_id,
        draft_id=draft_id,
        doc_token=ref.doc_token,
        title=content.title,
        doc_url=ref.url,
        local_path=str(path),
        content_hash=digest,
    )
    set_status(db, draft_id, "written", expected="creating_doc")
    return {
        "note_id": note_id,
        "doc_token": ref.doc_token,
        "doc_url": ref.url,
        "local_path": str(path),
    }


def build_ingest_graph(
    *,
    settings: Settings,
    llm: LLMClient,
    feishu: FeishuClient,
    checkpointer: Any = None,
    review_hook: ReviewHook | None = None,
    progress_hook: ProgressHook | None = None,
):
    """链路 A：知识点 -> 扩写 -> 评分 -> 人工审阅 -> 写入飞书。

    审阅拆成 prepare_review（发卡片/记事件）和 review（只做 interrupt）两个节点。
    LangGraph 在 resume 时会重新执行被打断的节点，把副作用放在前一个节点里，
    卡片和事件就不会发两遍。
    """
    db = settings.db_path
    judge_model = "fake-llm" if settings.use_fake_llm else settings.effective_judge_model

    def normalize(state: NoteState) -> dict[str, Any]:
        point = (state.get("raw_point") or "").strip()
        if not point:
            raise ValueError("知识点不能为空")
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="normalized",
            payload={"chars": len(point)},
        )
        return {"raw_point": point, "rewrite_count": int(state.get("rewrite_count") or 0)}

    def expand(state: NoteState) -> dict[str, Any]:
        feedback = state.get("feedback")
        content = generate_draft(llm, state["raw_point"], feedback=feedback)
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="expanded",
            payload={
                "title": content.title,
                "model": llm.model,
                "confidence": content.confidence,
                "with_feedback": bool(feedback),
            },
        )
        return {"content": content.model_dump(), "feedback": None}

    def critique_node(state: NoteState) -> dict[str, Any]:
        content = DraftContent.model_validate(state["content"])
        # 自检看的是最终排版文本，所以这里先渲染一次；render 节点再渲染一次是幂等的
        updated = critique(
            llm,
            point=state["raw_point"],
            markdown=render_blog(content),
            content=content,
        )
        return {"content": updated.model_dump()}

    def render_node(state: NoteState) -> dict[str, Any]:
        content = DraftContent.model_validate(state["content"])
        markdown = render_blog(content)
        write_draft_markdown(settings, state["draft_id"], markdown)
        update_draft(
            db,
            state["draft_id"],
            markdown=markdown,
            content_json=json.dumps(content.model_dump(), ensure_ascii=False),
            model=llm.model,
            prompt_version=EXPAND_PROMPT_VERSION,
        )
        return {"markdown": markdown}

    def evaluate_node(state: NoteState) -> dict[str, Any]:
        evaluation = evaluate_safely(llm, state["markdown"] or "", settings)
        save_evaluation(
            db,
            draft_id=state["draft_id"],
            scores=evaluation.scores,
            total=evaluation.total,
            judge_model=judge_model,
            prompt_version=EVALUATOR_PROMPT_VERSION,
            detail=evaluation.model_dump(),
        )
        update_draft(
            db, state["draft_id"], score=evaluation.total, verdict=evaluation.verdict
        )
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="evaluated",
            payload={"total": evaluation.total, "verdict": evaluation.verdict},
        )
        return {
            "evaluation": evaluation.model_dump(),
            "score": evaluation.total,
            "verdict": evaluation.verdict,
        }

    def auto_rewrite(state: NoteState) -> dict[str, Any]:
        """评分未达标时自动返工：计数 +1，把评估建议交给下一轮扩写。"""
        used = increment_rewrite(db, state["draft_id"])
        evaluation = state.get("evaluation") or {}
        feedback = build_feedback(evaluation, total=state.get("score"))
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="auto_rewrite",
            payload={
                "used": used,
                "max": settings.max_rewrites,
                "score": state.get("score"),
                "feedback": feedback[:1000],
            },
        )
        if progress_hook is not None:
            # 一轮返工要三次模型调用，CLI 里必须让用户看到还在动
            try:
                progress_hook(
                    {
                        "used": used,
                        "max": settings.max_rewrites,
                        "score": state.get("score"),
                    }
                )
            except Exception:  # noqa: BLE001 - 打印失败不能拖垮生成
                logger.debug("progress_hook 失败", exc_info=True)
        return {"rewrite_count": used, "feedback": feedback}

    def prepare_review(state: NoteState) -> dict[str, Any]:
        content = state.get("content") or {}
        markdown = state.get("markdown") or ""
        draft = get_draft(db, state["draft_id"]) or {}
        payload = {
            "draft_id": state["draft_id"],
            "title": content.get("title"),
            "score": state.get("score"),
            "verdict": state.get("verdict"),
            "pass_score": settings.pass_score,
            "evaluation": state.get("evaluation"),
            "excerpt": markdown[:400],
            "rewrite_count": int(draft.get("rewrite_count") or 0),
            "max_rewrites": settings.max_rewrites,
            "markdown_path": str(settings.data_dir / "drafts" / f"{state['draft_id']}.md"),
        }
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="awaiting_review",
            payload={
                "score": payload["score"],
                "verdict": payload["verdict"],
                "rewrite_count": payload["rewrite_count"],
            },
        )
        if review_hook is not None:
            review_hook(payload)

        # 卡片发不出去不该让草稿卡死在流程里：记事件、照常挂起，本地照样能审
        if settings.feishu_chat_id:
            review_url = f"{settings.review_base_url.rstrip('/')}/drafts/{state['draft_id']}"
            try:
                msg_id = feishu.send_review_card(
                    chat_id=settings.feishu_chat_id,
                    payload=payload,
                    review_url=review_url,
                )
            except Exception as exc:  # noqa: BLE001 - 外部服务失败不能拖垮审阅
                logger.warning("审阅卡片发送失败：%s", exc)
                add_event(
                    db,
                    draft_id=state["draft_id"],
                    type_="card_failed",
                    payload={"error": str(exc)[:300]},
                )
            else:
                if msg_id:
                    update_draft(db, state["draft_id"], review_msg_id=msg_id)
                add_event(
                    db,
                    draft_id=state["draft_id"],
                    type_="card_sent",
                    payload={"message_id": msg_id},
                )
        return {"review_payload": payload}

    def review_node(state: NoteState) -> dict[str, Any]:
        # 这里只做挂起，不要加任何副作用：resume 时整个函数会重跑
        decision = interrupt(state.get("review_payload") or {})
        if isinstance(decision, str):
            decision = {"action": decision}
        decision = decision or {}

        action = str(decision.get("action", "reject"))
        note = str(decision.get("note") or "").strip() or None

        if action == "approve":
            set_status(db, state["draft_id"], "approved", expected="awaiting_review")
            return {"review_action": "approve", "review_note": note}

        if action == "rewrite":
            draft = get_draft(db, state["draft_id"]) or {}
            used = int(draft.get("rewrite_count") or 0)
            if used >= settings.max_rewrites:
                set_status(db, state["draft_id"], "rejected", expected="awaiting_review")
                add_event(
                    db,
                    draft_id=state["draft_id"],
                    type_="rewrite_limit_reached",
                    payload={"used": used, "max": settings.max_rewrites},
                )
                return {
                    "review_action": "reject",
                    "review_note": "重写次数已用完，按拒绝处理",
                    "rewrite_count": used,
                }
            used = increment_rewrite(db, state["draft_id"])
            feedback = build_feedback(
                state.get("evaluation") or {}, total=state.get("score")
            )
            add_event(
                db,
                draft_id=state["draft_id"],
                type_="rewrite_requested",
                payload={"used": used},
            )
            return {
                "review_action": "rewrite",
                "review_note": note,
                "rewrite_count": used,
                "feedback": feedback,
            }

        set_status(db, state["draft_id"], "rejected", expected="awaiting_review")
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="rejected",
            payload={"note": note},
        )
        return {"review_action": "reject", "review_note": note}

    def write_feishu(state: NoteState) -> dict[str, Any]:
        draft_id = state["draft_id"]
        if state.get("note_id"):
            return {}
        try:
            return publish_draft(
                settings=settings, feishu=feishu, draft_id=draft_id
            )
        except PublishSkipped as exc:
            add_event(
                db,
                draft_id=draft_id,
                type_="write_skipped",
                payload={"status": exc.status},
            )
            return {"error": str(exc)}

    def commit(state: NoteState) -> dict[str, Any]:
        add_event(
            db,
            draft_id=state["draft_id"],
            type_="committed",
            payload={
                "note_id": state.get("note_id"),
                "doc_token": state.get("doc_token"),
            },
        )
        return {}

    def route_after_evaluate(state: NoteState) -> str:
        """不到分数线就自动返工，最多 max_rewrites 次；评估器自身故障直接交给人。"""
        if state.get("verdict") == "pass":
            return "prepare_review"
        evaluation = state.get("evaluation") or {}
        if evaluation.get("error"):
            return "prepare_review"
        used = int(state.get("rewrite_count") or 0)
        if used < settings.max_rewrites:
            return "auto_rewrite"
        return "prepare_review"

    def route_after_review(state: NoteState) -> str:
        action = state.get("review_action")
        if action == "approve":
            return "write_feishu"
        if action == "rewrite":
            return "expand"
        return END

    graph = StateGraph(NoteState)
    graph.add_node("normalize", normalize)
    graph.add_node("expand", expand)
    graph.add_node("critique", critique_node)
    graph.add_node("render", render_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("auto_rewrite", auto_rewrite)
    graph.add_node("prepare_review", prepare_review)
    graph.add_node("review", review_node)
    graph.add_node("write_feishu", write_feishu)
    graph.add_node("commit", commit)

    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "expand")
    graph.add_edge("expand", "critique")
    graph.add_edge("critique", "render")
    graph.add_edge("render", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluate,
        {"auto_rewrite": "auto_rewrite", "prepare_review": "prepare_review"},
    )
    graph.add_edge("auto_rewrite", "expand")
    graph.add_edge("prepare_review", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {"write_feishu": "write_feishu", "expand": "expand", END: END},
    )
    graph.add_edge("write_feishu", "commit")
    graph.add_edge("commit", END)

    return graph.compile(checkpointer=checkpointer)
