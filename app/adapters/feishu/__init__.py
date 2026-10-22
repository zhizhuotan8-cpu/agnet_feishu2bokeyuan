from __future__ import annotations

from app.config import Settings

from .base import DocRef, FeishuClient
from .fake import FakeFeishu


def get_feishu_client(settings: Settings) -> FeishuClient:
    if settings.feishu_mode == "fake":
        return FakeFeishu(settings)
    # 延迟导入：fake 模式不必加载 live 实现，跑测试时也不会碰网络依赖
    from .live import LiveFeishu

    return LiveFeishu(settings)


__all__ = ["DocRef", "FeishuClient", "FakeFeishu", "get_feishu_client"]
