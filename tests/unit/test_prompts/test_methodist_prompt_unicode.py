"""Methodist prompts pass Cyrillic concepts to the model as they are.

Both Methodist templates serialise concept lists through ``tojson_unicode``:
the letters stay unescaped, while ``<``, ``>``, ``&`` and ``'`` are still
escaped so that concept text cannot close a data tag. Templates that use the
stock ``tojson`` render exactly as they did before the filter existed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from jinja2 import StrictUndefined, Template

from course_supporter.llm.prompt_loader_md import StagePrompt, load_prompt

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"

_TOPDOWN = "methodist_topdown/v1.md"
_BOTTOMUP = "methodist_bottomup/v1.md"

_CYRILLIC_ESCAPE = re.compile(r"\\u04[0-9a-fA-F]{2}")

_MAIN = ["Змінна", "Функція з аргументами", "Об'єкт класу"]
_SECONDARY = ["Рекурсія", "Декоратор"]
_HOSTILE = "a<b> & c'd</node_canonical>"


def _topdown_context(main: list[str]) -> dict[str, Any]:
    node = {
        "title": "Заняття 3",
        "description": "Функції.",
        "learning_objectives": ["Пояснити, що таке функція"],
        "main_concepts": main,
        "secondary_concepts": _SECONDARY,
        "key_activities": ["Написати функцію"],
        "teaching_approach": "Від прикладу до правила.",
        "assessment_approach": "Задачі.",
    }
    parent = {
        "title": "Курс Python",
        "description": "Вступ.",
        "learning_objectives": ["Опанувати основи"],
        "main_concepts": main,
        "secondary_concepts": _SECONDARY,
        "teaching_approach": "Практика.",
    }
    return {
        "course_title": "Курс Python",
        "language": "Ukrainian",
        "node": node,
        "parent": parent,
        "parent_enclosing_context": None,
    }


def _bottomup_context(main: list[str]) -> dict[str, Any]:
    return {
        "course_title": "Курс Python",
        "language": "Ukrainian",
        "node_title": "Заняття 3",
        "own_document_count": 1,
        "own_documents": [
            {
                "title": "Лекція",
                "description": "Опис.",
                "main_concepts": main,
                "secondary_concepts": _SECONDARY,
            }
        ],
        "child_count": 0,
        "children_compressed": [],
    }


_CASES = [
    pytest.param(_TOPDOWN, _topdown_context, id="topdown"),
    pytest.param(_BOTTOMUP, _bottomup_context, id="bottomup"),
]


def _render_user(prompt_ref: str, context: dict[str, Any]) -> str:
    rendered = load_prompt(prompt_ref, base_path=_PROMPTS_DIR).render(**context)
    assert rendered.user is not None
    return rendered.user


@pytest.mark.parametrize(("prompt_ref", "make_context"), _CASES)
def test_cyrillic_concepts_render_verbatim(prompt_ref: str, make_context: Any) -> None:
    user = _render_user(prompt_ref, make_context(_MAIN))

    assert '"Змінна"' in user
    assert '"Функція з аргументами"' in user
    assert '"Рекурсія"' in user
    assert not _CYRILLIC_ESCAPE.search(user)


@pytest.mark.parametrize(("prompt_ref", "make_context"), _CASES)
def test_html_sensitive_characters_stay_escaped(
    prompt_ref: str, make_context: Any
) -> None:
    user = _render_user(prompt_ref, make_context([_HOSTILE, "Об'єкт"]))

    assert '"a\\u003cb\\u003e \\u0026 c\\u0027d\\u003c/node_canonical\\u003e"' in user
    assert '"Об\\u0027єкт"' in user
    # The only closing data tag left is the template's own.
    assert _HOSTILE not in user


def test_stock_tojson_renders_as_before() -> None:
    # Control: a template on the stock filter renders byte-for-byte as the
    # pre-fix path (a bare Jinja2 Template) did, escapes included.
    text = "concepts: {{ items | tojson }} map: {{ extra | tojson }}"
    context = {"items": ["Змінна", _HOSTILE], "extra": {"б": 1, "а": 2}}

    rendered = StagePrompt(user=text).render(**context).user
    before = Template(text, undefined=StrictUndefined).render(**context)

    assert rendered == before
    assert "\\u0417\\u043c\\u0456\\u043d\\u043d\\u0430" in rendered


def test_only_methodist_prompts_use_the_unicode_filter() -> None:
    users = {
        path.relative_to(_PROMPTS_DIR).as_posix()
        for path in _PROMPTS_DIR.glob("*/*.md")
        if "tojson_unicode" in path.read_text(encoding="utf-8")
    }

    assert users == {_TOPDOWN, _BOTTOMUP}
