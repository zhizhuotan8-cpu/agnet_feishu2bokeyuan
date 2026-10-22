from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path
from typing import Any

import typer

from app.config import get_settings
from app.graph.runner import resume_note, retry_write, start_note
from app.preview import DIMENSION_LABELS, STATUS_LABELS, write_static_preview
from app.store.db import (
    get_draft,
    get_note_by_draft,
    latest_evaluation,
    list_drafts,
    list_events,
)

app = typer.Typer(
    add_completion=False,
    help="知识点 -> 扩写 -> 评分 -> 人工审阅 -> 写入飞书（fake / live 两种模式）",
)


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _print_draft(draft: dict[str, Any]) -> None:
    settings = get_settings()
    evaluation = latest_evaluation(settings.db_path, draft["draft_id"]) or {}
    detail = _loads(evaluation.get("detail_json"))
    scores = _loads(evaluation.get("scores_json"))
    content = _loads(draft.get("content_json"))

    score = draft.get("score")
    score_text = f"{float(score):.1f}" if score is not None else "—"
    passed = score is not None and float(score) >= settings.pass_score
    mark = "达标" if passed else "未达标"
    color = typer.colors.GREEN if passed else typer.colors.RED

    typer.echo("")
    typer.secho(
        f"草稿 {draft['draft_id']}  ·  {score_text}/10  {mark}  ·  "
        f"{STATUS_LABELS.get(draft['status'], draft['status'])}",
        fg=color,
        bold=True,
    )
    if content.get("title"):
        typer.echo(f"标题  {content['title']}")

    if scores:
        parts = [
            f"{DIMENSION_LABELS.get(k, k)} {scores.get(k, 0)}"
            for k in DIMENSION_LABELS
        ]
        typer.echo("维度  " + " / ".join(parts))

    error = (detail.get("error") or "").strip()
    if error:
        typer.secho(f"评估失败  {error}", fg=typer.colors.RED)

    veto = (detail.get("veto") or "").strip()
    if veto:
        typer.secho(f"一票否决  {veto}", fg=typer.colors.RED)

    deductions = detail.get("deductions") or []
    if deductions:
        typer.echo(f"扣分项  {len(deductions)} 条")
        for item in deductions:
            if not isinstance(item, dict):
                continue
            label = DIMENSION_LABELS.get(str(item.get("dimension")), str(item.get("dimension")))
            typer.echo(f"  - {label} -{item.get('points', 0)}")
            quote = str(item.get("quote") or "").strip()
            if quote:
                typer.echo(f"    原文：{quote[:120]}")
            typer.echo(f"    原因：{item.get('reason', '')}")
            if quote and not item.get("quote_verified", True):
                typer.secho("    注意：引用没有在正文里找到，可能模型编了原句", fg=typer.colors.YELLOW)

    uncertain = content.get("uncertain_claims") or []
    if uncertain:
        typer.echo("待核实")
        for item in uncertain:
            typer.echo(f"  - {item}")

    typer.echo(f"稿件  {settings.data_dir / 'drafts' / (draft['draft_id'] + '.md')}")
    typer.echo(f"重写  已用 {draft.get('rewrite_count', 0)} / {settings.max_rewrites} 次")


def _print_next_steps(draft: dict[str, Any]) -> None:
    settings = get_settings()
    draft_id = draft["draft_id"]

    if draft["status"] == "awaiting_review":
        typer.echo("")
        typer.echo("下一步：")
        typer.echo(f"  note-agent approve {draft_id}    通过并写入飞书")
        typer.echo(f"  note-agent rewrite {draft_id}    重新生成一次")
        typer.echo(f"  note-agent reject {draft_id}     拒绝")
        typer.echo("  或者 note-agent serve 在浏览器里点。")
        return

    if draft["status"] == "written":
        note = get_note_by_draft(settings.db_path, draft_id) or {}
        mode = "fake" if settings.feishu_mode == "fake" else "live"
        typer.echo("")
        typer.secho(f"已写入飞书（{mode}）", fg=typer.colors.GREEN, bold=True)
        if note.get("doc_url"):
            typer.echo(f"  链接   {note['doc_url']}")
        if note.get("local_path"):
            typer.echo(f"  本地   {note['local_path']}")
        typer.echo(f"  hash   {note.get('content_hash', '')[:16]}…")
        return

    if draft["status"] == "approved":
        typer.echo("")
        typer.secho("上次写入没成功，草稿仍停在 approved。", fg=typer.colors.YELLOW)
        typer.echo(f"  note-agent retry-write {draft_id}    重试写入（不重新生成）")
        return

    if draft["status"] == "rejected":
        typer.echo("")
        typer.secho("这篇草稿已拒绝，没有写入飞书。", fg=typer.colors.YELLOW)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED)
    raise typer.Exit(code=1)


@app.command()
def new(
    point: str = typer.Argument(..., help="你写下的知识点"),
    draft_id: str | None = typer.Option(None, "--draft-id", help="复用指定 draft_id（调试用）"),
) -> None:
    """扩写一个知识点，跑到审阅挂起。"""
    def _progress(info: dict) -> None:
        typer.secho(
            f"评分 {info.get('score')} 未达标，自动返工 "
            f"{info.get('used')}/{info.get('max')} 次…",
            fg=typer.colors.YELLOW,
        )

    try:
        draft = start_note(point, draft_id=draft_id, progress_hook=_progress)
    except Exception as exc:  # noqa: BLE001 - CLI 要把错误翻译成人话
        _fail(f"生成失败：{exc}")
        return
    _print_draft(draft)
    _print_next_steps(draft)


@app.command()
def approve(draft_id: str = typer.Argument(...)) -> None:
    """通过审阅并写入飞书。"""
    try:
        draft = resume_note(draft_id, "approve")
    except Exception as exc:  # noqa: BLE001
        _fail(f"写入失败：{exc}")
        return
    _print_draft(draft)
    _print_next_steps(draft)


@app.command()
def reject(
    draft_id: str = typer.Argument(...),
    note: str = typer.Option("", "--note", help="拒绝原因，记进事件流"),
) -> None:
    """拒绝这篇草稿。"""
    try:
        draft = resume_note(draft_id, "reject", note=note)
    except Exception as exc:  # noqa: BLE001
        _fail(f"操作失败：{exc}")
        return
    _print_draft(draft)
    _print_next_steps(draft)


@app.command()
def rewrite(draft_id: str = typer.Argument(...)) -> None:
    """重写一次（不超过 MAX_REWRITES）。"""
    try:
        draft = resume_note(draft_id, "rewrite")
    except Exception as exc:  # noqa: BLE001
        _fail(f"重写失败：{exc}")
        return
    _print_draft(draft)
    _print_next_steps(draft)


@app.command("retry-write")
def retry_write_cmd(draft_id: str = typer.Argument(...)) -> None:
    """写入失败后重试（不重新生成、不重新评分）。"""
    try:
        draft = retry_write(draft_id)
    except LookupError as exc:
        _fail(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        _fail(f"重试写入失败：{exc}")
        return
    _print_draft(draft)
    _print_next_steps(draft)


@app.command("list")
def list_cmd(limit: int = typer.Option(20, "--limit")) -> None:
    """最近的草稿。"""
    settings = get_settings()
    rows = list_drafts(settings.db_path, limit=limit)
    if not rows:
        typer.echo("还没有草稿。先跑 note-agent new \"你的知识点\"。")
        return
    for row in rows:
        score = row.get("score")
        score_text = f"{float(score):4.1f}" if score is not None else "  — "
        status = STATUS_LABELS.get(row["status"], row["status"])
        point = (row.get("raw_point") or "").replace("\n", " ")[:40]
        typer.echo(f"{row['draft_id']}  {score_text}  {status:<8}  {point}")


@app.command()
def show(
    draft_id: str = typer.Argument(...),
    full: bool = typer.Option(False, "--full", help="打印完整 Markdown"),
    events: bool = typer.Option(False, "--events", help="打印事件流"),
) -> None:
    """查看草稿详情。"""
    settings = get_settings()
    draft = get_draft(settings.db_path, draft_id)
    if draft is None:
        _fail(f"草稿不存在：{draft_id}")
        return
    _print_draft(draft)

    if events:
        typer.echo("")
        typer.echo("事件流：")
        for event in list_events(settings.db_path, draft_id):
            typer.echo(f"  {event['created_at']}  {event['type']}  {event['payload_json']}")

    if full:
        typer.echo("")
        typer.echo(draft.get("markdown") or "")


@app.command()
def preview(
    draft_id: str = typer.Argument(...),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """导出静态预览页（不能点按钮，审阅请用 serve）。"""
    try:
        path = write_static_preview(get_settings(), draft_id)
    except LookupError as exc:
        _fail(str(exc))
        return
    typer.echo(f"预览页：{path}")
    if open_browser:
        webbrowser.open(Path(path).resolve().as_uri())


@app.command()
def serve(
    port: int = typer.Option(8000, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """启动审阅页（带通过/重写/拒绝按钮）。"""
    import uvicorn

    from app.main import app as fastapi_app

    url = f"http://{host}:{port}/"
    typer.echo(f"审阅页：{url}")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(fastapi_app, host=host, port=port)


@app.command("feishu-check")
def feishu_check(
    send_test: bool = typer.Option(False, "--send-test", help="往 FEISHU_CHAT_ID 发一条测试消息"),
) -> None:
    """检查飞书凭证、权限与配置（默认不发消息）。"""
    settings = get_settings()
    if settings.feishu_mode != "live":
        _fail("当前是 fake 模式：把 .env 里的 FEISHU_MODE 改成 live 再试")
        return

    from app.adapters.feishu.live import FeishuError, LiveFeishu

    try:
        client = LiveFeishu(settings)
        info = client.bot_info()
    except FeishuError as exc:
        _fail(f"飞书连通性检查失败：{exc}")
        return

    bot = info.get("bot") if isinstance(info.get("bot"), dict) else info
    typer.secho("飞书凭证 OK", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  应用   {bot.get('app_name') or '（接口未返回应用名）'}")
    typer.echo(f"  bot    {bot.get('open_id') or bot.get('app_id') or '—'}")
    typer.echo(
        "  落点   "
        + (settings.feishu_folder_token or "应用云空间根目录（需要配 FEISHU_OWNER_OPEN_ID 才能看到）")
    )
    typer.echo(f"  群     {settings.feishu_chat_id or '（未配置，不发审阅卡片）'}")
    typer.echo(
        f"  协作者 {settings.feishu_owner_open_id or '（未配置，文档只有应用可见）'}"
    )

    if send_test:
        if not settings.feishu_chat_id:
            _fail("--send-test 需要先配 FEISHU_CHAT_ID（note-agent feishu-chats 可以查）")
            return
        try:
            message_id = client.send_text(
                chat_id=settings.feishu_chat_id, text="note-agent 连通性测试，收到即权限正常"
            )
        except FeishuError as exc:
            _fail(f"发测试消息失败：{exc}")
            return
        typer.secho(f"测试消息已发送：{message_id}", fg=typer.colors.GREEN)


@app.command("feishu-chats")
def feishu_chats() -> None:
    """列出机器人所在的群（用来找 FEISHU_CHAT_ID）。"""
    settings = get_settings()
    if settings.feishu_mode != "live":
        _fail("当前是 fake 模式：把 .env 里的 FEISHU_MODE 改成 live 再试")
        return

    from app.adapters.feishu.live import FeishuError, LiveFeishu

    try:
        chats = LiveFeishu(settings).list_chats()
    except FeishuError as exc:
        _fail(f"列群失败：{exc}")
        return

    if not chats:
        typer.echo("机器人还没进任何群。先在飞书里把它拉进一个群，再运行一次。")
        return
    for chat in chats:
        typer.echo(
            f"{chat.get('chat_id')}  {chat.get('name') or '（未命名）'}  "
            f"{chat.get('description') or ''}".rstrip()
        )


@app.command("feishu-members")
def feishu_members(
    chat_id: str = typer.Option(
        "", "--chat-id", help="不填则用 .env 里的 FEISHU_CHAT_ID"
    )
) -> None:
    """列出群成员（open_id + 姓名），用来找 FEISHU_OWNER_OPEN_ID。"""
    settings = get_settings()
    if settings.feishu_mode != "live":
        _fail("当前是 fake 模式：把 .env 里的 FEISHU_MODE 改成 live 再试")
        return

    target = (chat_id or settings.feishu_chat_id).strip()
    if not target:
        _fail("没给群 id：先跑 note-agent feishu-chats 拿到 oc_...，再传 --chat-id")
        return

    from app.adapters.feishu.live import FeishuError, LiveFeishu

    try:
        members = LiveFeishu(settings).list_chat_members(target)
    except FeishuError as exc:
        _fail(f"读取群成员失败：{exc}")
        return

    if not members:
        typer.echo("这个群没有可见成员（机器人不在群里，或只有机器人自己）。")
        return
    typer.echo(f"群 {target} 的成员：")
    for member in members:
        typer.echo(
            f"  {member.get('member_id')}  {member.get('name') or '（未命名）'}"
        )
    typer.echo("")
    typer.echo("找到你自己那一行，把 member_id（ou_ 开头）填进 FEISHU_OWNER_OPEN_ID。")


@app.command("feishu-listen")
def feishu_listen(
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG / INFO / WARNING"),
) -> None:
    """启动卡片回调长连接（Ctrl+C 退出）。"""
    settings = get_settings()
    if settings.feishu_mode != "live":
        _fail("当前是 fake 模式：把 .env 里的 FEISHU_MODE 改成 live 再试")
        return

    from app.feishu_listener import run_listener

    typer.echo("正在建立飞书长连接…（需要开发者后台已订阅 card.action.trigger）")
    try:
        run_listener(settings, log_level=log_level)
    except KeyboardInterrupt:
        typer.echo("已退出")
    except RuntimeError as exc:
        _fail(str(exc))


if __name__ == "__main__":
    app()
