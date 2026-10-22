from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """让测试不读本机的 .env。

    app/config.py 在导入时就用 load_dotenv 把 `.env` 灌进了 os.environ，
    所以哪怕 Settings(env_file=None)，构造时仍会捡到本机真实配置——
    典型症状是本地填了 FEISHU_OWNER_OPEN_ID 之后，
    test_feishu_live 里没设协作者的用例突然多调了一次 permissions 接口。

    每个字段对应的环境变量都在用例期间摘掉；用例需要哪个值就显式传参，
    或者自己 monkeypatch.setenv（fixture 先跑，不会被这里清掉）。
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
