"""A stored explanation that does not read leaves no value in a log (DD-SP-DM).

``ValidationError`` of pydantic holds the value of every field that failed; the
value of a field of an explanation is the Mentor's words about the student's
work. These tests give a stored explanation a wrong type in a field whose value
is a marker string, and look for the marker where a value could have gone: the
builder's error, the log lines, the traceback of the exception the body raises,
and the line ``path_failed`` of the body — for this error and for any other
``ValidationError``.
"""

from __future__ import annotations

import traceback
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from pydantic import BaseModel, ValidationError
from structlog.testing import capture_logs

from course_supporter.homework import path_runner, text_result
from course_supporter.homework.path_runner import _fail_the_submission
from course_supporter.homework.result_builders import (
    PATH_FAILED,
    BuildContext,
    ResultNotBuiltError,
)
from course_supporter.homework.verdict_explanation import ExplanationAnswer
from course_supporter.models import stored_read
from tests._helpers.stored_review import MARKER, stored_explanation

_UNREADABLE = "stored_explanation_unreadable"


@pytest.fixture(autouse=True)
def _fresh_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The modules' loggers swapped for fresh ones.

    A logger bound by an earlier test is cached (``cache_logger_on_first_use``)
    with the processors of that test and would slip past ``capture_logs``.
    """
    for module in (text_result, path_runner, stored_read):
        monkeypatch.setattr(module, "logger", structlog.get_logger())


def _wrong_type() -> dict[str, Any]:
    """An explanation whose ``passed`` — a bool — holds the marker text."""
    stored = stored_explanation()
    stored["passed"] = MARKER
    return stored


def _unreadable(stored: dict[str, Any]) -> ValidationError:
    try:
        ExplanationAnswer.model_validate(stored)
    except ValidationError as error:
        return error
    msg = "the stored explanation reads"
    raise AssertionError(msg)


def _context(body: dict[str, Any]) -> tuple[BuildContext, Any]:
    submission = SimpleNamespace(tenant_id=uuid.uuid4(), id=uuid.uuid4())
    context = BuildContext(
        session=MagicMock(),
        session_factory=MagicMock(),
        submission=submission,  # type: ignore[arg-type]
        submission_text="work",
        review_language="ukr",
        redis=None,
    )
    verdicts = MagicMock()
    verdicts.list_for_submission = AsyncMock(return_value=[MagicMock()])
    verdicts.get_explanation = AsyncMock(
        return_value=SimpleNamespace(body=body, language="ukr")
    )
    return context, verdicts


async def _build_refused(body: dict[str, Any]) -> ResultNotBuiltError:
    context, verdicts = _context(body)
    with (
        patch.object(text_result, "SubmissionVerdictRepository", return_value=verdicts),
        pytest.raises(ResultNotBuiltError) as refused,
    ):
        await text_result.TextResultBuilder().build(context)
    return refused.value


class TestTheBoundaryOfTheBuilder:
    def test_pydantic_itself_puts_the_value_into_its_text(self) -> None:
        """The premise: without the boundary, the marker is in ``str(error)``."""
        assert MARKER in str(_unreadable(_wrong_type()))

    async def test_the_refusal_has_the_code_of_a_failure_on_our_side(self) -> None:
        refused = await _build_refused(_wrong_type())

        assert refused.code == PATH_FAILED == "path_failed"

    async def test_the_refusal_has_no_value_in_its_text(self) -> None:
        refused = await _build_refused(_wrong_type())

        assert MARKER not in str(refused)
        assert MARKER not in repr(refused)
        assert MARKER not in refused.details

    async def test_one_log_line_has_the_place_and_the_kind_never_the_value(
        self,
    ) -> None:
        with capture_logs() as logs:
            await _build_refused(_wrong_type())

        (line,) = [entry for entry in logs if entry["event"] == _UNREADABLE]
        assert line["problems"] == [{"loc": "passed", "type": "bool_parsing"}]
        assert line["whose"] == "explanation"
        assert MARKER not in repr(logs)

    async def test_the_traceback_of_the_exception_has_no_value(self) -> None:
        refused = await _build_refused(_wrong_type())

        trace = "".join(traceback.format_exception(refused))
        assert MARKER not in trace
        assert refused.__cause__ is None
        assert refused.__suppress_context__

    async def test_an_unknown_field_next_to_the_defect_does_not_bring_it_back(
        self,
    ) -> None:
        stored = _wrong_type()
        stored["from_a_newer_release"] = MARKER

        with capture_logs() as logs:
            refused = await _build_refused(stored)

        assert refused.code == PATH_FAILED
        assert MARKER not in repr(logs)
        assert MARKER not in "".join(traceback.format_exception(refused))

    async def test_the_earlier_attempts_explanation_is_read_at_the_same_boundary(
        self,
    ) -> None:
        from course_supporter.remark_kinds import RemarkState

        earlier = SimpleNamespace(submission_id=uuid.uuid4())
        verdicts = MagicMock()
        verdicts.earlier_attempt = AsyncMock(return_value=earlier)
        verdicts.get_explanation = AsyncMock(
            return_value=SimpleNamespace(body=_wrong_type())
        )
        submission = SimpleNamespace(tenant_id=uuid.uuid4(), id=uuid.uuid4())

        with (
            patch.object(
                text_result, "SubmissionVerdictRepository", return_value=verdicts
            ),
            capture_logs() as logs,
            pytest.raises(ResultNotBuiltError) as refused,
        ):
            await text_result._fixed_words(
                MagicMock(),
                submission,  # type: ignore[arg-type]
                [],
                states={"c1": RemarkState.FIXED},
            )

        assert refused.value.code == PATH_FAILED
        (line,) = [entry for entry in logs if entry["event"] == _UNREADABLE]
        assert line["whose"] == "earlier explanation"
        assert line["submission_id"] == str(earlier.submission_id)
        assert MARKER not in repr(logs)
        assert MARKER not in "".join(traceback.format_exception(refused.value))

    async def test_a_readable_explanation_is_read_in_silence(self) -> None:
        with capture_logs() as logs:
            read = text_result.read_stored_explanation(
                stored_explanation(), submission_id=uuid.uuid4(), whose="explanation"
            )

        assert read.why == "One requirement is not met."
        assert logs == []


class _Point(BaseModel):
    x: int


def _a_validation_error_of(value: str) -> ValidationError:
    try:
        _Point.model_validate({"x": value})
    except ValidationError as error:
        return error
    msg = "the point reads"
    raise AssertionError(msg)


class _SessionFactory:
    """``async_sessionmaker`` enough for ``_fail_the_submission``."""

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *_: object) -> None:
        return None


async def _failed_with(exc: Exception) -> tuple[list[dict[str, Any]], AsyncMock, Any]:
    """What ``_fail_the_submission`` logs, writes and tells the webhook."""
    repo = MagicMock()
    repo.update_status = AsyncMock()
    notify = AsyncMock()
    with (
        patch.object(path_runner, "HomeworkRepository", return_value=repo),
        patch.object(path_runner, "_notify_failed", notify),
        capture_logs() as logs,
    ):
        await _fail_the_submission(_SessionFactory(), uuid.uuid4(), exc)  # type: ignore[arg-type]
    return logs, repo.update_status, notify


class TestThePathFailedLine:
    async def test_a_validation_error_has_its_type_and_its_places_only(self) -> None:
        error = _a_validation_error_of(MARKER)

        logs, _, _ = await _failed_with(error)

        (line,) = [entry for entry in logs if entry["event"] == "path_failed"]
        assert line["error"] == "ValidationError"
        assert line["problems"] == [{"loc": "x", "type": "int_parsing"}]
        assert MARKER not in repr(logs)

    async def test_a_validation_error_fails_the_submission_as_any_other_does(
        self,
    ) -> None:
        _, update_status, notify = await _failed_with(_a_validation_error_of(MARKER))

        assert update_status.await_args.kwargs["error_message"] == PATH_FAILED
        assert notify.await_args.kwargs["reason"] == PATH_FAILED

    async def test_a_refusal_of_the_builder_keeps_its_code_and_its_line(self) -> None:
        refusal = await _build_refused(_wrong_type())

        logs, update_status, notify = await _failed_with(refusal)

        (line,) = [entry for entry in logs if entry["event"] == "path_failed"]
        assert line["code"] == PATH_FAILED
        assert line["error"].startswith("ResultNotBuiltError: path_failed: ")
        assert MARKER not in repr(logs)
        assert update_status.await_args.kwargs["error_message"] == PATH_FAILED
        assert notify.await_args.kwargs["reason"] == PATH_FAILED

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("the broker is away"), ValueError("a defect")],
        ids=["runtime", "value"],
    )
    async def test_another_exception_is_written_as_it_always_was(
        self, exc: Exception
    ) -> None:
        logs, update_status, _ = await _failed_with(exc)

        (line,) = [entry for entry in logs if entry["event"] == "path_failed"]
        assert line["error"] == f"{type(exc).__name__}: {exc}"
        assert "problems" not in line
        assert update_status.await_args.kwargs["error_message"] == PATH_FAILED
