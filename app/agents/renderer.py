from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, meta

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "blog_post.md.j2"


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def template_source() -> str:
    return (TEMPLATE_DIR / TEMPLATE_NAME).read_text(encoding="utf-8")


def template_variables() -> set[str]:
    """模板引用的顶层变量，用来和 schema 字段做对齐测试。"""
    ast = _env().parse(template_source())
    return set(meta.find_undeclared_variables(ast))


def render_blog(content: Any) -> str:
    if hasattr(content, "model_dump"):
        data = content.model_dump()
    else:
        data = dict(content)
    return _env().get_template(TEMPLATE_NAME).render(**data).strip() + "\n"
