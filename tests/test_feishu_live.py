from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.feishu.live import (
    FeishuAuthError,
    FeishuError,
    FeishuImportError,
    LiveFeishu,
)
from app.config import Settings


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "feishu_mode": "live",
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "secret",
        "data_dir": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)


def _ok(data: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "ok", "data": data or {}})


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_missing_credentials_raises(tmp_path) -> None:
    with pytest.raises(FeishuAuthError):
        LiveFeishu(_settings(tmp_path, feishu_app_id="", feishu_app_secret=""))


def test_create_doc_happy_path_and_collaborator(tmp_path) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append({"method": request.method, "path": path, "body": request.content})
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "t-1",
                    "expire": 7200,
                },
            )
        if path.endswith("/medias/upload_all"):
            assert b"ccm_import_open" in request.content
            return _ok({"file_token": "file_1"})
        if path.endswith("/import_tasks"):
            body = json.loads(request.content)
            assert body["file_extension"] == "md"
            assert body["type"] == "docx"
            assert body["point"] == {"mount_type": 1, "mount_key": "fld_1"}
            return _ok({"ticket": "tk_1"})
        if "/import_tasks/" in path:
            return _ok(
                {
                    "result": {
                        "job_status": 0,
                        "token": "docx_1",
                        "url": "https://feishu.cn/docx/docx_1",
                    }
                }
            )
        if "/permissions/" in path:
            body = json.loads(request.content)
            assert body["member_type"] == "openid"
            assert body["perm"] == "full_access"
            assert request.url.params.get("type") == "docx"
            return _ok()
        raise AssertionError(f"未预期的请求：{path}")

    settings = _settings(
        tmp_path, feishu_folder_token="fld_1", feishu_owner_open_id="ou_owner"
    )
    feishu = LiveFeishu(settings, client=_client(handler))
    ref = feishu.create_doc_from_markdown(
        title="GIL 是什么", markdown="# 标题\n\n正文\n", draft_id="d_1"
    )

    assert ref.doc_token == "docx_1"
    assert ref.url == "https://feishu.cn/docx/docx_1"
    assert ref.warning is None
    token_calls = [c for c in calls if c["path"].endswith("tenant_access_token/internal")]
    assert len(token_calls) == 1, "token 应该缓存，不该每次请求都换"


def test_import_retries_until_success(tmp_path) -> None:
    clock = Clock()
    statuses = [2, 1, 0]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if path.endswith("/medias/upload_all"):
            return _ok({"file_token": "file_1"})
        if path.endswith("/import_tasks"):
            body = json.loads(request.content)
            # mount_key 必填，根目录用空字符串（省略会被飞书判成 99992402）
            assert body["point"] == {"mount_type": 1, "mount_key": ""}
            return _ok({"ticket": "tk_1"})
        if "/import_tasks/" in path:
            status = statuses.pop(0)
            payload = {"job_status": status}
            if status == 0:
                payload.update({"token": "docx_1", "url": "https://feishu.cn/docx/docx_1"})
            return _ok({"result": payload})
        raise AssertionError(path)

    feishu = LiveFeishu(
        _settings(tmp_path), client=_client(handler), sleep=clock.sleep, clock=clock
    )
    ref = feishu.create_doc_from_markdown(title="t", markdown="x", draft_id="d")
    assert ref.doc_token == "docx_1"
    assert clock.now == 2.0, "两次 pending 各等 1 秒"


def test_import_failure_is_terminal(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if path.endswith("/medias/upload_all"):
            return _ok({"file_token": "file_1"})
        if path.endswith("/import_tasks"):
            body = json.loads(request.content)
            # mount_key 必填，根目录用空字符串（省略会被飞书判成 99992402）
            assert body["point"] == {"mount_type": 1, "mount_key": ""}
            return _ok({"ticket": "tk_1"})
        return _ok({"result": {"job_status": 112, "job_error_msg": "格式不支持"}})

    feishu = LiveFeishu(_settings(tmp_path), client=_client(handler))
    with pytest.raises(FeishuImportError) as excinfo:
        feishu.create_doc_from_markdown(title="t", markdown="x", draft_id="d")
    assert "格式不支持" in str(excinfo.value)
    assert excinfo.value.code == 112


def test_token_invalid_triggers_refresh(tmp_path) -> None:
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            tokens.append("new")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{len(tokens)}",
                    "expire": 7200,
                },
            )
        auth = request.headers.get("authorization")
        if auth == "Bearer t-1":
            return httpx.Response(200, json={"code": 99991663, "msg": "invalid token"})
        return _ok({"items": [], "has_more": False})

    feishu = LiveFeishu(_settings(tmp_path), client=_client(handler))
    assert feishu.read_markdown("docx_1") == ""
    assert len(tokens) == 2


def test_api_error_surfaces_code_and_message(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        return httpx.Response(200, json={"code": 1069902, "msg": "no permission"})

    feishu = LiveFeishu(_settings(tmp_path), client=_client(handler))
    with pytest.raises(FeishuError) as excinfo:
        feishu.revoke("docx_1")
    assert excinfo.value.code == 1069902
    assert "no permission" in str(excinfo.value)


def test_reject_unsafe_doc_token(tmp_path) -> None:
    feishu = LiveFeishu(_settings(tmp_path), client=_client(lambda r: _ok()))
    with pytest.raises(FeishuError):
        feishu.revoke("../../etc/passwd")


def test_list_chat_members_paginates(tmp_path) -> None:
    pages = [
        {"items": [{"member_id": "ou_1", "name": "我"}], "has_more": True, "page_token": "p2"},
        {"items": [{"member_id": "ou_2", "name": "同事"}], "has_more": False},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        assert request.url.params.get("member_id_type") == "open_id"
        return _ok(pages.pop(0))

    feishu = LiveFeishu(_settings(tmp_path), client=_client(handler))
    members = feishu.list_chat_members("oc_1")
    assert [m["member_id"] for m in members] == ["ou_1", "ou_2"]


def test_send_review_card_posts_interactive_message(tmp_path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        body = json.loads(request.content)
        seen.update(body)
        assert request.url.params.get("receive_id_type") == "chat_id"
        return _ok({"message_id": "om_1"})

    feishu = LiveFeishu(_settings(tmp_path), client=_client(handler))
    payload = {
        "draft_id": "d_1",
        "title": "标题",
        "score": 8.0,
        "verdict": "pass",
        "pass_score": 8.0,
        "evaluation": {"scores": {"accuracy": 8}},
        "excerpt": "正文预览",
        "rewrite_count": 0,
        "max_rewrites": 3,
    }
    message_id = feishu.send_review_card(
        chat_id="oc_1", payload=payload, review_url="http://127.0.0.1:8000/drafts/d_1"
    )
    assert message_id == "om_1"
    assert seen["msg_type"] == "interactive"
    card = json.loads(seen["content"])
    assert card["header"]["template"] == "green"
