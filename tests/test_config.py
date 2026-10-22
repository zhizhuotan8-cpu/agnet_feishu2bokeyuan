from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT, Settings


def test_env_var_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("PASS_SCORE", "7.5")
    assert Settings().pass_score == 7.5


def test_bool_env_var_is_parsed(monkeypatch) -> None:
    monkeypatch.setenv("FAKE_LLM", "true")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    settings = Settings()
    assert settings.fake_llm is True
    assert settings.use_fake_llm is True


def test_relative_data_dir_is_anchored_to_project_root() -> None:
    settings = Settings(data_dir=Path("tmp-data"))
    assert settings.data_dir == PROJECT_ROOT / "tmp-data"
    assert settings.db_path == PROJECT_ROOT / "tmp-data" / "app.db"
    assert settings.graph_db_path == PROJECT_ROOT / "tmp-data" / "graph.sqlite"


def test_absolute_data_dir_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "data"
    assert Settings(data_dir=target).data_dir == target


def test_judge_model_falls_back_to_main_model() -> None:
    settings = Settings(llm_model="deepseek-chat", judge_model="")
    assert settings.effective_judge_model == "deepseek-chat"
    assert Settings(llm_model="a", judge_model="b").effective_judge_model == "b"


def test_missing_api_key_falls_back_to_fake_llm() -> None:
    assert Settings(llm_api_key="", fake_llm=False).use_fake_llm is True
    assert Settings(llm_api_key="sk-x", fake_llm=False).use_fake_llm is False
