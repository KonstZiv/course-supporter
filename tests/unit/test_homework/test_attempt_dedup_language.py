"""The review's language in the key of a copy, without a database (V-6, 2026-11-23).

Two pieces of the rule are not a query's business and are locked here; the
choice of the source — "the latest attempt IN the language" — is a query, and is
locked on a real database (``tests/integration/test_attempt_dedup_db.py``, the
classes ``TestLanguageLock…``):

* the language of a submission is the doors' own answer
  (``path_runner.door_languages``: the form, else the student's stored language,
  else the task's), never the raw ``response_language`` — read here through the
  real code over rows the session hands back;
* the repository's query carries the condition when a language is asked for, and
  carries none when it is not (the price of a work not yet sent).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from course_supporter.homework.attempt_dedup import _review_language_of
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument, HomeworkSubmission, Student


class _Rows:
    """A session that hands back the rows a lookup by primary key asks for."""

    def __init__(
        self,
        *,
        form: str | None,
        stored: str | None,
        task: str | None,
        submission_exists: bool = True,
    ) -> None:
        self._rows: dict[type, Any] = {
            HomeworkSubmission: (
                SimpleNamespace(
                    student_id=uuid.uuid4(),
                    authored_document_id=uuid.uuid4(),
                    response_language=form,
                )
                if submission_exists
                else None
            ),
            Student: SimpleNamespace(preferred_language=stored),
            AuthoredDocument: SimpleNamespace(language=task),
        }

    async def get(self, model: type, _key: object) -> Any:
        return self._rows[model]


class TestTheLanguageOfASubmission:
    async def test_the_form_comes_first_in_any_of_its_codes(self) -> None:
        rows = _Rows(form="en", stored="uk", task="ukr")

        assert await _review_language_of(rows, uuid.uuid4()) == "eng"  # type: ignore[arg-type]

    async def test_a_form_that_names_nothing_leaves_the_student_s_language(
        self,
    ) -> None:
        """The raw field is empty; the answer is the stored language, in the
        canonical code — not ``None`` and not the task's language."""
        rows = _Rows(form=None, stored="en", task="ukr")

        assert await _review_language_of(rows, uuid.uuid4()) == "eng"  # type: ignore[arg-type]

    async def test_neither_names_one_the_task_s_language_decides(self) -> None:
        rows = _Rows(form=None, stored=None, task="ukr")

        assert await _review_language_of(rows, uuid.uuid4()) == "ukr"  # type: ignore[arg-type]

    async def test_a_value_that_resolves_to_nothing_is_skipped(self) -> None:
        rows = _Rows(form="not-a-language", stored=None, task="ukr")

        assert await _review_language_of(rows, uuid.uuid4()) == "ukr"  # type: ignore[arg-type]

    async def test_no_source_gives_none(self) -> None:
        rows = _Rows(form=None, stored=None, task=None)

        assert await _review_language_of(rows, uuid.uuid4()) is None  # type: ignore[arg-type]

    async def test_a_submission_that_is_gone_gives_none(self) -> None:
        rows = _Rows(form="uk", stored="uk", task="ukr", submission_exists=False)

        assert await _review_language_of(rows, uuid.uuid4()) is None  # type: ignore[arg-type]


class _CapturedStatement:
    """A session that keeps the statement it was given and finds no row."""

    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> Any:
        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=lambda: None)


async def _compiled_lookup(**extra: Any) -> tuple[str, dict[str, Any]]:
    session = _CapturedStatement()
    await HomeworkRepository(session).latest_unchanged_attempt(  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        student_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        excluding=uuid.uuid4(),
        fingerprint="f" * 64,
        task_version="a" * 64,
        criteria_list=None,
        evaluator_version="ev1:" + "e" * 16,
        **extra,
    )
    compiled = session.statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


class TestTheQueryOfTheSource:
    async def test_a_language_asked_for_is_a_condition_of_the_choice(self) -> None:
        sql, params = await _compiled_lookup(review_language="ukr")

        assert "review_result ->> %(review_result_1)s" in sql
        assert params["review_result_1"] == "language"
        assert "ukr" in params.values()
        # One statement, one choice: the condition is in its WHERE, ahead of the
        # ORDER BY and the LIMIT, so "the latest" is the latest in the language.
        where, _, tail = sql.partition("ORDER BY")
        assert "review_result ->>" in where.rsplit("WHERE", 1)[1]
        assert "LIMIT" in tail

    async def test_no_language_asked_for_is_no_condition(self) -> None:
        sql, params = await _compiled_lookup()

        assert "review_result ->>" not in sql
        assert "language" not in params.values()


@pytest.mark.parametrize("language", ["ukr", "eng"])
async def test_the_language_asked_for_is_the_one_bound(language: str) -> None:
    _, params = await _compiled_lookup(review_language=language)

    assert language in params.values()
