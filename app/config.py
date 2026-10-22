from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 固定按项目根目录找 .env，不受启动时 cwd 影响（IDE、计划任务、别的目录里跑 CLI 都能读到）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# 已经存在的环境变量优先于 .env：CI / 临时覆盖时不用改文件
load_dotenv(ENV_FILE, override=False, encoding="utf-8")


class Settings(BaseSettings):
    # 关掉 pydantic-settings 自带的 dotenv 读取，只保留上面这一条加载路径，
    # 免得两个加载器对 `.env` 的理解不一致。环境变量仍然由 os.environ 提供。
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
    )

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    judge_model: str = ""
    request_timeout: float = 60.0

    data_dir: Path = Path("data")
    pass_score: float = 8.0
    max_rewrites: int = 3

    feishu_mode: Literal["fake", "live"] = "fake"
    fake_llm: bool = False

    # 飞书自建应用凭证。FAKE 模式下不用填；LIVE 模式缺了会在创建客户端时直接报错
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # 导入目标文件夹：空 = 应用云空间根目录。填了就别漏 folder 的写权限
    feishu_folder_token: str = ""
    # 可选：文档建好后把「完全访问」授给这个人，否则 tenant token 建的文档你看不到
    feishu_owner_open_id: str = ""
    # 审阅卡片发到这个群（oc_ 开头）。留空则不发卡片，继续用本地 CLI/网页
    feishu_chat_id: str = ""
    # 卡片里的「看全文」链接；默认本机预览页，手机打不开就配局域网 IP 或内网穿透
    review_base_url: str = "http://127.0.0.1:8000"
    # 私有化部署 / 测试替身用；一般不用改
    feishu_base_url: str = "https://open.feishu.cn"

    @field_validator("data_dir", mode="after")
    @classmethod
    def _anchor_data_dir(cls, value: Path) -> Path:
        """DATA_DIR 写相对路径时按项目根目录算，不跟 cwd 跑。"""
        return value if value.is_absolute() else (PROJECT_ROOT / value)

    @property
    def effective_judge_model(self) -> str:
        return self.judge_model or self.llm_model

    @property
    def db_path(self) -> Path:
        """业务表：草稿、评分、笔记、发布记录。"""
        return self.data_dir / "app.db"

    @property
    def graph_db_path(self) -> Path:
        """LangGraph checkpoint 单独放，避免两套表混在一个文件里。"""
        return self.data_dir / "graph.sqlite"

    @property
    def use_fake_llm(self) -> bool:
        """没配 key 时自动回落，保证 clone 下来就能跑通流程。"""
        return self.fake_llm or not self.llm_api_key

    def ensure_dirs(self) -> None:
        for sub in ("drafts", "notes", "fake_feishu", "snapshots"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试里改环境变量后调用。"""
    get_settings.cache_clear()
