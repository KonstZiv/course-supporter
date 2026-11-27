"""Б-2: the pure rules of the student deletion — the neutral file name, the
reason, the rate-limit counters it deletes."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import ExternalServiceCall
from course_supporter.student_deletion import (
    DeletionRefusedError,
    neutral_key,
    neutral_name,
)
from course_supporter.student_deletion.file_names import name_replacer
from course_supporter.student_deletion.service import (
    _NAME_TARGETS,
    EMAIL_COUNTERS,
    STUDENT_COUNTERS,
    _name_change,
    _NameChange,
    _replace_names,
    check_reason,
)

ROOT = Path(__file__).resolve().parents[2]
TEST_ANSWERS_FILENAME = "answers.json"
"""``homework.submission_core.TEST_ANSWERS_FILENAME``; that module cannot be
the first one imported (an import cycle of its own), so it is not imported."""


@pytest.mark.parametrize(
    ("original", "neutral"),
    [
        ("Ivanenko_dz3.py", "submission.py"),
        ("Ivanenko HW.PDF", "submission.pdf"),
        ("project-ivanenko.tar.gz", "submission.tar.gz"),
        ("project.TGZ", "submission.tgz"),
        ("work.zip", "submission.zip"),
        ("README", "submission"),
        # An extension the upload door does not accept may be a word of the name.
        ("homework.ivanenko", "submission"),
    ],
)
def test_the_neutral_name_keeps_only_a_known_extension(
    original: str, neutral: str
) -> None:
    assert neutral_name(original) == neutral


def test_the_key_keeps_its_folder_and_takes_the_neutral_name() -> None:
    key = "homework/t-1/s-1/Ivanenko_dz3.py"
    assert neutral_key(key, "Ivanenko_dz3.py") == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


def test_a_name_with_slashes_stays_in_its_submission_folder() -> None:
    key = "homework/t-1/s-1/Users/ivanenko/hw.py"
    assert neutral_key(key, "Users/ivanenko/hw.py") == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


def test_a_tests_answers_keep_the_platforms_own_name() -> None:
    key = f"homework/t-1/s-1/{TEST_ANSWERS_FILENAME}"
    assert neutral_key(key, TEST_ANSWERS_FILENAME) is None


def test_an_already_neutral_key_is_left_alone() -> None:
    assert neutral_key("homework/t-1/s-1/submission.py", "submission.py") is None


def test_without_an_original_name_the_key_names_the_file() -> None:
    assert neutral_key("homework/t-1/s-1/Ivanenko.py", None) == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


@pytest.mark.parametrize("reason", ["", "   ", "asked by ivan@example.org", "x@y"])
def test_a_reason_that_is_empty_or_holds_an_address_is_refused(reason: str) -> None:
    with pytest.raises(DeletionRefusedError):
        check_reason(reason)


def test_a_reason_is_stripped() -> None:
    assert check_reason("  ticket 42 ") == "ticket 42"


def test_the_counters_deleted_are_the_ones_the_routes_keep() -> None:
    """The deletion names the rate-limit counters by the routes' limit names;
    a renamed limit would leave its counter behind, so the names are checked
    against the route that counts them."""
    routes = (ROOT / "src/course_supporter/api/routes/portal_join.py").read_text()
    for name in (*EMAIL_COUNTERS, *STUDENT_COUNTERS):
        assert f'"{name}"' in routes, name


# ── The old name, replaced in texts (доповнення 1) ─────────────────────────

_REPLACE = {"Ivanenko_dz3.py": "submission.py", "a.py": "submission.py"}


@pytest.mark.parametrize(
    ("before", "after", "found"),
    [
        ("Ivanenko_dz3.py", "submission.py", 1),
        ("*файл Ivanenko_dz3.py:12-14*", "*файл submission.py:12-14*", 1),
        ("see a.py.", "see submission.py.", 1),
        ("(a.py, a.py)", "(submission.py, submission.py)", 2),
        # Part of a longer name or word: not the file.
        ("data.py", "data.py", 0),
        ("a.pyc", "a.pyc", 0),
        ("a.py.bak", "a.py.bak", 0),
        ("my-a.py", "my-a.py", 0),
        # After a path separator: a path inside an archive, the work's.
        ("src/a.py:3", "src/a.py:3", 0),
        ("src\\a.py", "src\\a.py", 0),
    ],
)
def test_only_the_whole_name_standing_alone_is_replaced(
    before: str, after: str, found: int
) -> None:
    assert name_replacer(_REPLACE).text(before) == (after, found)


def test_json_keys_and_values_are_rewritten_and_counted() -> None:
    value = {
        "Ivanenko_dz3.py": {"lines": [1, 2]},
        "remarks": [{"position": {"value": "a.py:1"}}, None, 3, True],
    }
    assert name_replacer(_REPLACE).value(value) == (
        {
            "submission.py": {"lines": [1, 2]},
            "remarks": [{"position": {"value": "submission.py:1"}}, None, 3, True],
        },
        2,
    )


def test_nothing_to_replace_finds_nothing() -> None:
    assert name_replacer({"submission.py": "submission.py"}).text("submission.py") == (
        "submission.py",
        0,
    )


class _FakeSession:
    """In-memory AsyncSession stand-in recording compiled UPDATE statements."""

    def __init__(self, rowcount: int = 1) -> None:
        self._rowcount = rowcount
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any) -> SimpleNamespace:
        self.updates.append((stmt.table.name, dict(stmt.compile().params)))
        return SimpleNamespace(rowcount=self._rowcount)


async def test_replace_names_rewrites_plain_text_remark_place_and_json_string() -> None:
    replacer = name_replacer({"Ivanenko_dz3.py": "submission.py"})
    target = next(t for t in _NAME_TARGETS if t.model is ExternalServiceCall)
    row_id = uuid.uuid4()
    change = _name_change(
        target,
        (
            row_id,
            "Review of Ivanenko_dz3.py completed.",
            '{"quote_file": "Ivanenko_dz3.py"}',
            "Ivanenko_dz3.py:12",
        ),
        replacer,
    )
    assert change is not None

    session = _FakeSession()
    await _replace_names(cast(AsyncSession, session), [change])

    assert session.updates == [
        (
            "external_service_calls",
            {
                "input_text": "Review of submission.py completed.",
                "output_text": '{"quote_file": "submission.py"}',
                "error_message": "submission.py:12",
                "id_1": row_id,
            },
        )
    ]


@pytest.mark.parametrize("text", ["my_Ivanenko_dz3.py_old", "src/Ivanenko_dz3.py"])
async def test_replace_names_ignores_word_parts_and_archive_paths(text: str) -> None:
    replacer = name_replacer({"Ivanenko_dz3.py": "submission.py"})
    rewritten, found = replacer.text(text)
    assert (rewritten, found) == (text, 0)
    assert rewritten is text

    target = next(t for t in _NAME_TARGETS if t.model is ExternalServiceCall)
    assert _name_change(target, (uuid.uuid4(), text, None, None), replacer) is None

    session = _FakeSession()
    await _replace_names(cast(AsyncSession, session), [])
    assert session.updates == []


async def test_replace_names_replaces_multiple_and_keeps_clean_text() -> None:
    replacer = name_replacer({"Ivanenko_dz3.py": "submission.py"})
    clean = "No student file name is mentioned here."
    clean_out, clean_found = replacer.text(clean)
    assert clean_found == 0
    assert clean_out is clean

    target = next(t for t in _NAME_TARGETS if t.model is ExternalServiceCall)
    row_id = uuid.uuid4()
    change = _name_change(
        target,
        (row_id, "First Ivanenko_dz3.py, then Ivanenko_dz3.py:12.", clean, None),
        replacer,
    )
    assert change is not None
    assert change.occurrences == {"input_text": 2}

    session = _FakeSession()
    await _replace_names(cast(AsyncSession, session), [change])

    assert session.updates == [
        (
            "external_service_calls",
            {
                "input_text": "First submission.py, then submission.py:12.",
                "id_1": row_id,
            },
        )
    ]


async def test_replace_names_refuses_when_row_disappeared() -> None:
    row_id = uuid.uuid4()
    change = _NameChange(
        ExternalServiceCall,
        row_id,
        {ExternalServiceCall.input_text: "submission.py"},
        {"input_text": 1},
    )
    session = _FakeSession(rowcount=0)
    with pytest.raises(DeletionRefusedError, match="changed under the run"):
        await _replace_names(cast(AsyncSession, session), [change])
