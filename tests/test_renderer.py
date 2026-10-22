from __future__ import annotations

from app.agents.llm import FakeLLM
from app.agents.prompts import EXPAND_SYSTEM, EXPAND_USER
from app.agents.renderer import render_blog, template_variables
from app.agents.schemas import DraftContent


def _content() -> DraftContent:
    raw = FakeLLM().chat_json(EXPAND_SYSTEM, EXPAND_USER.format(point="Python 的 GIL"))
    return DraftContent.model_validate(raw)


# confidence 是给审阅页看的内部元信息，不进要发布的正文
NOT_IN_TEMPLATE = {"confidence"}


def test_template_matches_schema_fields() -> None:
    # 模板里引用的变量必须都能在 schema 里找到；schema 的字段除了明确排除的，也都该被用上
    assert template_variables() == set(DraftContent.model_fields) - NOT_IN_TEMPLATE


def test_render_contains_all_sections() -> None:
    markdown = render_blog(_content())
    assert markdown.startswith("# ")
    for heading in (
        "## 一、先说结论",
        "## 二、为什么是这样",
        "## 三、动手试一下",
        "## 四、几个容易踩的坑",
        "## 五、小结",
    ):
        assert heading in markdown
    assert "```python" in markdown
    assert "待核实" in markdown
    assert "**标签**：" in markdown


def test_refs_render_one_per_line() -> None:
    # 模板里 if/endif 紧贴行尾时 trim_blocks 会吃掉换行，曾经把两条参考挤成一行
    content = DraftContent.model_validate(
        {
            **_content().model_dump(),
            "refs": [
                {"title": "Python 官方文档：threading 模块", "url": None},
                {"title": "PEP 703", "url": "https://peps.python.org/pep-0703/"},
            ],
        }
    )
    markdown = render_blog(content)
    assert "\n- Python 官方文档：threading 模块\n" in markdown
    assert "\n- [PEP 703](https://peps.python.org/pep-0703/)\n" in markdown


def test_uncertain_claims_render_as_list() -> None:
    content = DraftContent.model_validate(
        {**_content().model_dump(), "uncertain_claims": ["第一条。", "第二条"]}
    )
    markdown = render_blog(content)
    assert "\n> - 第一条。\n> - 第二条\n" in markdown


def test_render_is_deterministic() -> None:
    content = _content()
    assert render_blog(content) == render_blog(content.model_dump())


def test_render_omits_empty_refs() -> None:
    content = _content()
    assert content.refs == []
    assert "## 参考" not in render_blog(content)
