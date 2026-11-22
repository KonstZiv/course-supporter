"""BIL-B4: the size of what a key's explanations are shown, one rule."""

from __future__ import annotations

import json
from datetime import date

import pytest

from course_supporter.billing.estimates import key_explanation_task_chars


@pytest.mark.parametrize(
    ("text", "answers"),
    [
        ("1. Що виведе print(2 ** 3)?\nа) 6\nб) 8", {"1": ["б"]}),
        ("", {}),
        ("Тест", {"1": ["а", "в"], "складено": date(2026, 11, 17)}),
    ],
)
def test_the_worker_line_before_bil_b4(text: str, answers: object) -> None:
    """The number the worker computed inline: Cyrillic escaped by ``json``,
    a date written by ``default=str``."""
    assert key_explanation_task_chars(text, answers) == len(text) + len(
        json.dumps(answers, default=str)
    )


def test_a_cyrillic_answer_counts_as_json_escapes_it() -> None:
    """A Cyrillic letter in the key is six characters of JSON, not one."""
    assert key_explanation_task_chars("", ["б"]) == len('["\\u0431"]') == 10
