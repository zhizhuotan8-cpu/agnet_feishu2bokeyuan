from __future__ import annotations

import ast

from app.agents.schemas import DraftContent

PYTHON_LANGS = {"python", "py", "python3"}


def validate_code(content: DraftContent) -> None:
    """代码必须能过语法检查；过不了直接算生成失败，不交给评估器。"""
    lang = (content.example_lang or "").strip().lower()
    if lang in PYTHON_LANGS:
        try:
            ast.parse(content.example_code)
        except SyntaxError as exc:
            raise ValueError(f"example_code 语法错误：{exc}") from exc
        return

    # 其他语言只做最基本的围栏/括号配对检查
    code = content.example_code
    if code.count("(") != code.count(")"):
        raise ValueError("example_code 括号不配对")
    if code.count("{") != code.count("}"):
        raise ValueError("example_code 花括号不配对")
