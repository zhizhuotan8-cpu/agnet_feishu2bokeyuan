"""飞书 docx blocks → Markdown。

只覆盖模板会用到的块：标题、正文、有序/无序列表、代码、引用、待办、分隔线。
表格和图片没有稳定的一期方案（设计文档 3.3 / 4.3），这里显式降级成注释，
让发布前能一眼看到「哪里没同步」，而不是悄悄丢掉。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

BLOCK_PAGE = 1
BLOCK_TEXT = 2
BLOCK_HEADING = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 11: 9}
BLOCK_BULLET = 12
BLOCK_ORDERED = 13
BLOCK_CODE = 14
BLOCK_QUOTE = 15
BLOCK_TODO = 17
BLOCK_DIVIDER = 22
BLOCK_IMAGE = 27
BLOCK_TABLE = 31
BLOCK_QUOTE_CONTAINER = 34

BLOCK_KEYS = {
    BLOCK_PAGE: "page",
    BLOCK_TEXT: "text",
    BLOCK_BULLET: "bullet",
    BLOCK_ORDERED: "ordered",
    BLOCK_CODE: "code",
    BLOCK_QUOTE: "quote",
    BLOCK_TODO: "todo",
    BLOCK_DIVIDER: "divider",
    BLOCK_IMAGE: "image",
    BLOCK_TABLE: "table",
    BLOCK_QUOTE_CONTAINER: "quote_container",
    # block_type 3~11 分别是 heading1~heading9，key 不是层级而是 block_type
    **{block_type: f"heading{level}" for block_type, level in BLOCK_HEADING.items()},
}

_TRAILING_BLANK = re.compile(r"\n{3,}")


def _payload(block: dict[str, Any]) -> dict[str, Any]:
    block_type = block.get("block_type")
    key = BLOCK_KEYS.get(block_type) if isinstance(block_type, int) else None
    value = block.get(key) if key else None
    return value if isinstance(value, dict) else {}


def _text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in _payload(block).get("elements") or []:
        if not isinstance(element, dict):
            continue
        if "text_run" in element:
            parts.append(str((element["text_run"] or {}).get("content") or ""))
        elif "mention_doc" in element:
            parts.append(str((element["mention_doc"] or {}).get("title") or ""))
        elif "equation" in element:
            parts.append(str((element["equation"] or {}).get("content") or ""))
    return "".join(parts)


def _children(block: dict[str, Any], by_id: dict[str, dict[str, Any]], depth: int) -> list[str]:
    lines: list[str] = []
    ordered = 0
    in_list = False
    for child_id in block.get("children") or []:
        child = by_id.get(str(child_id))
        if child is None:
            continue
        block_type = child.get("block_type")
        text = _text(child).strip()
        nested = child.get("children") or []
        indent = "  " * depth

        if block_type == BLOCK_ORDERED or block_type == BLOCK_BULLET:
            if block_type == BLOCK_ORDERED:
                ordered += 1
                marker = f"{ordered}."
            else:
                ordered = 0
                marker = "-"
            in_list = True
            lines.append(f"{indent}{marker} {text}")
            if nested:
                lines.extend(_children(child, by_id, depth + 1))
            continue

        # 列表后面的块要空一行，否则围栏代码块会被当成列表项的延续
        if in_list:
            lines.append("")
            in_list = False
        ordered = 0
        lines.extend(_block(child, by_id, depth))
    return lines


def _block(block: dict[str, Any], by_id: dict[str, dict[str, Any]], depth: int) -> list[str]:
    block_type = block.get("block_type")
    text = _text(block)
    nested = block.get("children") or []

    if block_type in BLOCK_HEADING:
        return [f"{'#' * BLOCK_HEADING[block_type]} {text}", ""]

    if block_type == BLOCK_TEXT:
        return ([text, ""] if text else []) + (
            _children(block, by_id, depth) if nested else []
        )

    if block_type == BLOCK_CODE:
        # 飞书的语言枚举没有可靠的公开映射，宁可给无语言围栏，也不写错语言
        return ["```", text.rstrip("\n"), "```", ""]

    if block_type == BLOCK_QUOTE:
        quoted = [f"> {line}" if line else ">" for line in text.splitlines() or [""]]
        return quoted + ([""] if text else [])

    if block_type == BLOCK_TODO:
        style = _payload(block).get("style") or {}
        done = "x" if isinstance(style, dict) and style.get("done") else " "
        return [f"- [{done}] {text}"]

    if block_type == BLOCK_DIVIDER:
        return ["---", ""]

    if block_type == BLOCK_IMAGE:
        token = str(_payload(block).get("token") or "")
        return [f"<!-- 图片未同步：{token or 'unknown'} -->", ""]

    if block_type == BLOCK_TABLE:
        return ["<!-- 表格未同步：飞书表格转 Markdown 在链路上会丢结构 -->", ""]

    # page / quote_container / callout / grid 这类容器：内容都在子块里
    if nested:
        return _children(block, by_id, depth)
    if text:
        return [text, ""]
    return []


def blocks_to_markdown(items: Iterable[dict[str, Any]]) -> str:
    blocks = [item for item in items if isinstance(item, dict)]
    if not blocks:
        return ""
    by_id = {str(block.get("block_id")): block for block in blocks}
    roots = [block for block in blocks if not block.get("parent_id")]
    root = roots[0] if roots else blocks[0]

    lines = _children(root, by_id, 0)
    text = "\n".join(lines).strip()
    return _TRAILING_BLANK.sub("\n\n", text) + "\n" if text else ""
