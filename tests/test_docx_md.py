from __future__ import annotations

from app.adapters.feishu.docx_md import blocks_to_markdown


def _text_block(block_id: str, text: str, block_type: int = 2, **extra) -> dict:
    key = {2: "text", 12: "bullet", 13: "ordered", 15: "quote", 17: "todo", 14: "code"}[block_type]
    return {
        "block_id": block_id,
        "block_type": block_type,
        "parent_id": "page",
        "children": [],
        key: {"elements": [{"text_run": {"content": text}}], **extra},
    }


def test_renders_headings_lists_code_and_quote() -> None:
    items = [
        {"block_id": "page", "block_type": 1, "parent_id": "",
         "children": ["h", "p", "b", "o1", "o2", "code", "quote", "div"]},
        {"block_id": "h", "block_type": 3, "parent_id": "page",
         "heading1": {"elements": [{"text_run": {"content": "标题"}}]}},
        _text_block("p", "正文第一段"),
        _text_block("b", "要点", 12),
        _text_block("o1", "第一步", 13),
        _text_block("o2", "第二步", 13),
        _text_block("code", "print(1)\n", 14),
        _text_block("quote", "引用一句话", 15),
        {"block_id": "div", "block_type": 22, "parent_id": "page", "divider": {}},
    ]

    markdown = blocks_to_markdown(items)

    assert markdown.startswith("# 标题\n")
    assert "正文第一段" in markdown
    assert "- 要点" in markdown
    assert "1. 第一步" in markdown and "2. 第二步" in markdown
    assert "```\nprint(1)\n```" in markdown
    assert "> 引用一句话" in markdown
    assert "---" in markdown


def test_nested_bullets_are_indented() -> None:
    items = [
        {"block_id": "page", "block_type": 1, "parent_id": "", "children": ["b1"]},
        {"block_id": "b1", "block_type": 12, "parent_id": "page", "children": ["b2"],
         "bullet": {"elements": [{"text_run": {"content": "外层"}}]}},
        {"block_id": "b2", "block_type": 12, "parent_id": "b1", "children": [],
         "bullet": {"elements": [{"text_run": {"content": "内层"}}]}},
    ]
    markdown = blocks_to_markdown(items)
    assert "- 外层" in markdown
    assert "  - 内层" in markdown


def test_block_after_nested_list_gets_blank_line() -> None:
    # 没有空行的话，围栏代码块会被 Markdown 解析成列表项的延续
    items = [
        {"block_id": "page", "block_type": 1, "parent_id": "", "children": ["b1", "code"]},
        {"block_id": "b1", "block_type": 12, "parent_id": "page", "children": ["b2"],
         "bullet": {"elements": [{"text_run": {"content": "外层"}}]}},
        {"block_id": "b2", "block_type": 12, "parent_id": "b1", "children": [],
         "bullet": {"elements": [{"text_run": {"content": "内层"}}]}},
        _text_block("code", "print(1)\n", 14),
    ]
    markdown = blocks_to_markdown(items)
    assert "  - 内层\n\n```\nprint(1)\n```" in markdown


def test_table_and_image_are_marked_not_silently_dropped() -> None:
    items = [
        {"block_id": "page", "block_type": 1, "parent_id": "", "children": ["t", "img"]},
        {"block_id": "t", "block_type": 31, "parent_id": "page", "children": [],
         "table": {"cells": []}},
        {"block_id": "img", "block_type": 27, "parent_id": "page", "children": [],
         "image": {"token": "file_abc"}},
    ]
    markdown = blocks_to_markdown(items)
    assert "表格未同步" in markdown
    assert "图片未同步：file_abc" in markdown


def test_empty_input() -> None:
    assert blocks_to_markdown([]) == ""
