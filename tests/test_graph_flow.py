from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.adapters.feishu.fake import FakeFeishu
from app.agents.llm import FakeLLM, LLMError
from app.config import Settings
from app.graph.note_ingest import build_ingest_graph
from app.graph.runner import reset_graphs, resume_note, retry_write, start_note
from app.store.db import (
    create_draft,
    get_draft,
    get_note_by_draft,
    init_db,
    list_events,
    latest_evaluation,
    set_status,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        fake_llm=True,
        pass_score=8.0,
        max_rewrites=1,
        feishu_mode="fake",
    )


@pytest.fixture(autouse=True)
def _reset_graphs():
    reset_graphs()
    yield
    reset_graphs()


def _event_types(settings: Settings, draft_id: str) -> list[str]:
    return [e["type"] for e in list_events(settings.db_path, draft_id)]


def test_fake_llm_reaches_pass_line() -> None:
    llm = FakeLLM()
    result = llm.chat_json(
        "TASK:EVALUATE",
        "请评审下面这篇文章：\n\n# 标题\n\n正文",
    )
    total = sum(
        {"accuracy": 0.3, "specificity": 0.2, "density": 0.2, "structure": 0.15, "readability": 0.15}[k]
        * v
        for k, v in result["scores"].items()
    )
    assert round(total, 1) >= 8.0, result["scores"]


def test_start_then_approve_writes_note(settings: Settings) -> None:
    draft = start_note("Python 的 GIL 到底是什么", settings=settings)
    draft_id = draft["draft_id"]

    assert draft["status"] == "awaiting_review"
    assert float(draft["score"]) >= 8.0
    assert draft["verdict"] == "pass"

    markdown_path = settings.data_dir / "drafts" / f"{draft_id}.md"
    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    for heading in ("## 一、先说结论", "## 二、为什么是这样", "## 三、动手试一下", "## 五、小结"):
        assert heading in markdown
    assert "```python" in markdown

    evaluation = latest_evaluation(settings.db_path, draft_id)
    assert evaluation["judge_model"] == "fake-llm"
    assert json.loads(evaluation["scores_json"])["accuracy"] == 8

    result = resume_note(draft_id, "approve", settings=settings)
    assert result["status"] == "written"

    note = get_note_by_draft(settings.db_path, draft_id)
    assert note is not None
    assert Path(note["local_path"]).exists()
    assert note["content_hash"]

    written = list((settings.data_dir / "fake_feishu").glob("*.md"))
    assert len(written) == 1
    assert "Python 的 GIL" in written[0].read_text(encoding="utf-8")

    types = _event_types(settings, draft_id)
    for expected in ("normalized", "expanded", "evaluated", "awaiting_review", "committed"):
        assert expected in types


def test_double_approve_is_blocked(settings: Settings) -> None:
    draft = start_note("重复点击测试", settings=settings)
    resume_note(draft["draft_id"], "approve", settings=settings)
    with pytest.raises(RuntimeError):
        resume_note(draft["draft_id"], "approve", settings=settings)


class BrokenFeishu(FakeFeishu):
    def create_doc_from_markdown(self, *, title: str, markdown: str, draft_id: str):
        raise RuntimeError("模拟导入失败")


def test_retry_write_recovers_from_failed_import(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = start_note("重试写入测试", settings=settings)
    draft_id = draft["draft_id"]
    # 模拟上一次写入失败后回滚到 approved
    assert set_status(settings.db_path, draft_id, "approved", expected="awaiting_review")

    monkeypatch.setattr(
        "app.graph.runner.get_feishu_client", lambda s: BrokenFeishu(s)
    )
    with pytest.raises(RuntimeError):
        retry_write(draft_id, settings=settings)
    assert get_draft(settings.db_path, draft_id)["status"] == "approved"
    assert "write_failed" in _event_types(settings, draft_id)

    monkeypatch.setattr(
        "app.graph.runner.get_feishu_client", lambda s: FakeFeishu(s)
    )
    result = retry_write(draft_id, settings=settings)
    assert result["status"] == "written"
    assert get_note_by_draft(settings.db_path, draft_id) is not None

    # 已经写过就不能再重试，否则会建出第二篇
    with pytest.raises(RuntimeError):
        retry_write(draft_id, settings=settings)


def test_reject_leaves_no_note(settings: Settings) -> None:
    draft = start_note("拒绝路径测试", settings=settings)
    result = resume_note(draft["draft_id"], "reject", note="内容太浅", settings=settings)

    assert result["status"] == "rejected"
    assert get_note_by_draft(settings.db_path, draft["draft_id"]) is None
    assert not list((settings.data_dir / "fake_feishu").glob("*.md"))
    assert "rejected" in _event_types(settings, draft["draft_id"])


def test_rewrite_then_limit(settings: Settings) -> None:
    draft = start_note("重写路径测试", settings=settings)
    draft_id = draft["draft_id"]
    first_score = float(draft["score"])

    again = resume_note(draft_id, "rewrite", settings=settings)
    assert again["status"] == "awaiting_review"
    assert again["rewrite_count"] == 1
    assert float(again["score"]) == first_score  # FakeLLM 是确定性的
    assert "rewrite_requested" in _event_types(settings, draft_id)

    with pytest.raises(RuntimeError):
        resume_note(draft_id, "rewrite", settings=settings)


class LowScoreLLM(FakeLLM):
    """把五维全部压到 5 分，用来验证低分闸门和强行通过。"""

    def chat_json(self, system: str, user: str, **kwargs):  # type: ignore[override]
        if "TASK:EVALUATE" in system:
            return {
                "scores": {
                    "accuracy": 5,
                    "specificity": 5,
                    "density": 5,
                    "structure": 5,
                    "readability": 5,
                },
                "deductions": [
                    {
                        "dimension": "accuracy",
                        "points": 5.0,
                        "quote": "先给结论",
                        "reason": "测试桩：内容没有可验证的事实。",
                    }
                ],
                "veto": None,
            }
        return super().chat_json(system, user, **kwargs)


def test_low_score_still_waits_for_human_and_can_be_forced(settings: Settings) -> None:
    settings.ensure_dirs()
    init_db(settings.db_path)

    graph = build_ingest_graph(
        settings=settings,
        llm=LowScoreLLM(),
        feishu=FakeFeishu(settings),
        checkpointer=MemorySaver(),
    )
    draft = create_draft(
        settings.db_path, raw_point="低分闸门测试", template_version="blog_v1"
    )
    draft_id = draft["draft_id"]
    config = {"configurable": {"thread_id": draft_id}, "recursion_limit": 50}

    graph.invoke(
        {"draft_id": draft_id, "raw_point": "低分闸门测试", "rewrite_count": 0},
        config,
    )

    row = get_draft(settings.db_path, draft_id)
    assert row["status"] == "awaiting_review"
    assert float(row["score"]) == 5.0
    assert row["verdict"] == "below_bar"
    # fixture 的 max_rewrites=1：自动返工一轮仍然不到 8 分，就交给人
    assert int(row["rewrite_count"]) == 1
    assert _event_types(settings, draft_id).count("auto_rewrite") == 1

    # 分数不到 8 也允许人强行通过
    graph.invoke(Command(resume={"action": "approve"}), config)
    assert get_draft(settings.db_path, draft_id)["status"] == "written"


class ImprovingLLM(FakeLLM):
    """前两轮低分、第三轮达标，并且记录每次扩写收到的 prompt。"""

    def __init__(self, scores: tuple[int, ...] = (6, 6, 9)) -> None:
        self._scores = list(scores)
        self.eval_calls = 0
        self.expand_prompts: list[str] = []

    def chat_json(self, system: str, user: str, **kwargs):  # type: ignore[override]
        if "TASK:CRITIQUE" in system:
            return super().chat_json(system, user, **kwargs)
        if "TASK:EVALUATE" in system:
            score = self._scores[min(self.eval_calls, len(self._scores) - 1)]
            self.eval_calls += 1
            return {
                "scores": {name: score for name in (
                    "accuracy", "specificity", "density", "structure", "readability"
                )},
                "deductions": [
                    {
                        "dimension": "specificity",
                        "points": 2.0,
                        "quote": "先给结论",
                        "reason": "测试桩：没有真实数字。",
                    }
                ],
                "suggestions": ["补充一个真实可运行的例子", "给出具体数字"],
                "veto": None,
            }
        self.expand_prompts.append(user)
        return super().chat_json(system, user, **kwargs)


def test_below_bar_triggers_auto_rewrite_then_review(settings: Settings) -> None:
    settings = settings.model_copy(update={"max_rewrites": 3})
    settings.ensure_dirs()
    init_db(settings.db_path)

    llm = ImprovingLLM()
    graph = build_ingest_graph(
        settings=settings,
        llm=llm,
        feishu=FakeFeishu(settings),
        checkpointer=MemorySaver(),
    )
    draft = create_draft(
        settings.db_path, raw_point="自动返工测试", template_version="blog_v1"
    )
    draft_id = draft["draft_id"]

    graph.invoke(
        {"draft_id": draft_id, "raw_point": "自动返工测试", "rewrite_count": 0},
        {"configurable": {"thread_id": draft_id}, "recursion_limit": 50},
    )

    row = get_draft(settings.db_path, draft_id)
    assert row["status"] == "awaiting_review"
    assert row["verdict"] == "pass"
    assert float(row["score"]) == 9.0
    assert int(row["rewrite_count"]) == 2
    assert len(llm.expand_prompts) == 3
    # 第二、三轮扩写必须带上评估器给的改稿建议
    assert "上一版得分 6.0/10" in llm.expand_prompts[1]
    assert "补充一个真实可运行的例子" in llm.expand_prompts[1]
    assert _event_types(settings, draft_id).count("auto_rewrite") == 2


class ErroringEvalLLM(FakeLLM):
    def chat_json(self, system: str, user: str, **kwargs):  # type: ignore[override]
        if "TASK:EVALUATE" in system:
            raise LLMError("模拟评估器崩了")
        return super().chat_json(system, user, **kwargs)


class DecliningLLM(FakeLLM):
    """分数一轮比一轮低：三版都不过线，第一版反而最好。"""

    def __init__(self, scores: tuple[int, ...] = (7, 6, 5)) -> None:
        self._scores = list(scores)
        self.eval_calls = 0
        self.expand_calls = 0

    def chat_json(self, system: str, user: str, **kwargs):  # type: ignore[override]
        if "TASK:CRITIQUE" in system:
            return super().chat_json(system, user, **kwargs)
        if "TASK:EVALUATE" in system:
            score = self._scores[min(self.eval_calls, len(self._scores) - 1)]
            self.eval_calls += 1
            return {
                "scores": {
                    name: score
                    for name in (
                        "accuracy",
                        "specificity",
                        "density",
                        "structure",
                        "readability",
                    )
                },
                "deductions": [],
                "suggestions": ["补充一个真实可运行的例子"],
                "veto": None,
            }
        draft = super().chat_json(system, user, **kwargs)
        self.expand_calls += 1
        # 每版留个标记，方便断言最后被审的是哪一版
        draft["title"] = f"{draft['title']}（第 {self.expand_calls} 版）"
        return draft


def test_best_version_goes_to_review_when_all_attempts_miss(
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"max_rewrites": 2})
    settings.ensure_dirs()
    init_db(settings.db_path)

    llm = DecliningLLM()
    reviews: list[dict] = []
    graph = build_ingest_graph(
        settings=settings,
        llm=llm,
        feishu=FakeFeishu(settings),
        checkpointer=MemorySaver(),
        review_hook=reviews.append,
    )
    draft = create_draft(
        settings.db_path, raw_point="取最高分测试", template_version="blog_v1"
    )
    draft_id = draft["draft_id"]
    config = {"configurable": {"thread_id": draft_id}, "recursion_limit": 50}

    graph.invoke(
        {"draft_id": draft_id, "raw_point": "取最高分测试", "rewrite_count": 0},
        config,
    )

    row = get_draft(settings.db_path, draft_id)
    assert row["status"] == "awaiting_review"
    assert row["verdict"] == "below_bar"
    # 三版分别是 7 / 6 / 5 分，交给人的必须是 7 分那版
    assert float(row["score"]) == 7.0
    assert int(row["rewrite_count"]) == 2
    assert llm.expand_calls == 3
    assert "第 1 版" in row["markdown"]
    assert "第 3 版" not in row["markdown"]

    assert reviews, "应该发一次审阅"
    payload = reviews[-1]
    assert payload["fallback_to_best"] is True
    assert payload["attempts"] == 3
    assert payload["best_attempt"] == 1
    assert float(payload["score"]) == 7.0
    assert "review_best_of" in _event_types(settings, draft_id)

    # 通过后写进飞书的也必须是这一版，不是最后一版
    graph.invoke(Command(resume={"action": "approve"}), config)
    note = get_note_by_draft(settings.db_path, draft_id)
    assert note is not None
    assert "第 1 版" in Path(note["local_path"]).read_text(encoding="utf-8")


def test_evaluator_error_skips_auto_rewrite(settings: Settings) -> None:
    settings = settings.model_copy(update={"max_rewrites": 3})
    settings.ensure_dirs()
    init_db(settings.db_path)

    graph = build_ingest_graph(
        settings=settings,
        llm=ErroringEvalLLM(),
        feishu=FakeFeishu(settings),
        checkpointer=MemorySaver(),
    )
    draft = create_draft(
        settings.db_path, raw_point="评估器异常测试", template_version="blog_v1"
    )
    draft_id = draft["draft_id"]

    graph.invoke(
        {"draft_id": draft_id, "raw_point": "评估器异常测试", "rewrite_count": 0},
        {"configurable": {"thread_id": draft_id}, "recursion_limit": 50},
    )

    row = get_draft(settings.db_path, draft_id)
    # 评估器自己坏了，重写也不会有反馈可用，直接挂起等人看
    assert row["status"] == "awaiting_review"
    assert int(row["rewrite_count"]) == 0
    assert "auto_rewrite" not in _event_types(settings, draft_id)
