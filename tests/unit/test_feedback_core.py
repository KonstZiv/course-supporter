"""The core that decides a touch (mentor-rebuild task 05).

No database here: what this module decides — which refusal, in which order,
with which tenant — is decided before any row is written, and a double for the
repository shows it more sharply than a live table would. The storage side has
its own live tests (``tests/integration/test_feedback_repository_db.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.homework import feedback_core
from course_supporter.homework.feedback_core import (
    NO_REVIEW_CODE,
    SUBMISSION_NOT_FOUND,
    touch_review,
)
from course_supporter.storage.orm import HomeworkSubmission

_STUDENT_ID = uuid.UUID("01a0b000-0000-7000-8000-000000000001")
_TENANT_ID = uuid.UUID("01a0b000-0000-7000-8000-000000000002")


def _submission(
    *,
    status: str = "completed",
    review_markdown: str | None = "# Review\n\nWell done.",
    student_id: uuid.UUID = _STUDENT_ID,
    deleted_at: datetime | None = None,
) -> HomeworkSubmission:
    """A submission double — a real ORM object, never persisted."""
    submission = HomeworkSubmission(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        student_id=student_id,
        course_node_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
        delivery_mode="in_app",
    )
    submission.status = status
    submission.review_markdown = review_markdown
    submission.deleted_at = deleted_at
    return submission


def _session() -> AsyncSession:
    return MagicMock(spec=AsyncSession)


@contextmanager
def _patched_repo() -> Iterator[MagicMock]:
    """Patch the repository with a WORKING double.

    ``record`` is an ``AsyncMock`` even in the tests that expect a refusal: a
    double that cannot be awaited would turn "the core failed to refuse" into a
    ``TypeError``, and a red from a broken double proves only that Python can
    read the file. With this, a missing check shows up as the assertion it is —
    the refusal did not happen.
    """
    with patch.object(feedback_core, "FeedbackRepository") as repo_cls:
        repo_cls.return_value.record = AsyncMock(return_value="stored-row")
        yield repo_cls


async def _touch(
    submission: HomeworkSubmission | None,
    *,
    value: FeedbackValue = FeedbackValue.HELPED,
) -> tuple[Any, MagicMock]:
    """Run the core with a doubled repository; return (result, repo class)."""
    with _patched_repo() as repo_cls:
        result = await touch_review(
            _session(),
            submission=submission,
            student_id=_STUDENT_ID,
            value=value,
        )
    return result, repo_cls


class TestTheOrderOfTheTwoRefusals:
    async def test_no_submission_is_a_generic_not_found(self) -> None:
        """Nothing to answer about → 404, and nothing is written."""
        with (
            _patched_repo() as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            await touch_review(
                _session(),
                submission=None,
                student_id=_STUDENT_ID,
                value=FeedbackValue.HELPED,
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == SUBMISSION_NOT_FOUND
        repo_cls.assert_not_called()

    async def test_a_foreign_submission_without_a_review_is_not_found_not_a_conflict(
        self,
    ) -> None:
        """The leak this order exists to prevent, measured in the core itself.

        The submission is real, it belongs to somebody else, and it has no
        review. If the review were checked first the answer would be 409, and
        409 says "this exists": the requester would learn about a submission
        they have no access to, which invariant 4 of the package forbids. The
        assertion is indistinguishability — the same status and the same body
        as for an id that never existed.
        """
        with _patched_repo() as repo_cls:
            with pytest.raises(HTTPException) as unknown_info:
                await touch_review(
                    _session(),
                    submission=None,
                    student_id=_STUDENT_ID,
                    value=FeedbackValue.HELPED,
                )
            with pytest.raises(HTTPException) as foreign_info:
                await touch_review(
                    _session(),
                    submission=_submission(
                        status="received",
                        review_markdown=None,
                        student_id=uuid.uuid4(),
                    ),
                    student_id=_STUDENT_ID,
                    value=FeedbackValue.NOT_HELPED,
                )
        unknown, foreign = unknown_info.value, foreign_info.value

        assert foreign.status_code == 404
        assert (foreign.status_code, foreign.detail) == (
            unknown.status_code,
            unknown.detail,
        )
        repo_cls.assert_not_called()

    async def test_a_reviewed_submission_of_another_student_is_not_found(self) -> None:
        """Ownership is checked in the core, not only at the door.

        A reviewed submission that belongs to someone else: the refusal must be
        the generic one, and nothing may be written — least of all a row under
        the owner's name, which is what a core that trusted its caller would do.
        """
        with (
            _patched_repo() as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            await touch_review(
                _session(),
                submission=_submission(student_id=uuid.uuid4()),
                student_id=_STUDENT_ID,
                value=FeedbackValue.HELPED,
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == SUBMISSION_NOT_FOUND
        repo_cls.assert_not_called()

    async def test_a_soft_deleted_submission_is_not_found(self) -> None:
        """Liveness is checked in the core too: a deleted row answers nothing."""
        with (
            _patched_repo() as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            await touch_review(
                _session(),
                submission=_submission(deleted_at=datetime.now(UTC)),
                student_id=_STUDENT_ID,
                value=FeedbackValue.HELPED,
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == SUBMISSION_NOT_FOUND
        repo_cls.assert_not_called()


class TestARefusalWhenThereIsNoReviewToAnswerAbout:
    @pytest.mark.parametrize(
        ("status", "review_markdown", "case"),
        [
            ("received", None, "still in flight"),
            ("reviewing", None, "still in flight, further along"),
            ("completed", None, "terminal, no review written"),
            ("completed", "", "terminal, empty review text"),
            ("completed", "   \n  ", "terminal, blank review text"),
            ("mismatch", "# Review", "terminal, but not a reviewed state"),
        ],
    )
    async def test_the_same_code_for_every_shape_of_no_review(
        self, status: str, review_markdown: str | None, case: str
    ) -> None:
        """One refusal, whatever the reason there is no review to answer about."""
        with (
            _patched_repo() as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            await touch_review(
                _session(),
                submission=_submission(status=status, review_markdown=review_markdown),
                student_id=_STUDENT_ID,
                value=FeedbackValue.HELPED,
            )
        assert exc_info.value.status_code == 409, case
        assert isinstance(exc_info.value.detail, dict)
        assert exc_info.value.detail["code"] == NO_REVIEW_CODE
        assert exc_info.value.detail["details"], "a code with no sentence beside it"
        repo_cls.assert_not_called()


class TestARecordedTouch:
    @pytest.mark.parametrize("status", ["completed", "delivered"])
    async def test_both_reviewed_statuses_are_answerable(self, status: str) -> None:
        """The two milestones in which a review was actually written."""
        submission = _submission(status=status)
        result, repo_cls = await _touch(submission)
        assert result == "stored-row"
        repo_cls.return_value.record.assert_awaited_once()

    async def test_the_row_takes_its_owner_and_tenant_from_the_submission(
        self,
    ) -> None:
        """Not from the request beside it: the row carries its target's own.

        The core has no tenant argument at all, and the student it writes is the
        submission's owner rather than the one it was handed — the two were just
        checked against each other, so what this pins is where the value comes
        from, not which value it is today.
        """
        submission = _submission()
        _, repo_cls = await _touch(submission, value=FeedbackValue.NOT_HELPED)

        kwargs = repo_cls.return_value.record.await_args.kwargs
        assert kwargs == {
            "tenant_id": submission.tenant_id,
            "student_id": _STUDENT_ID,
            "target_kind": FeedbackTargetKind.REVIEW,
            "target_id": submission.id,
            "kind": FeedbackKind.TOUCH,
            "value": FeedbackValue.NOT_HELPED,
        }


class TestTheReadablePredicate:
    @pytest.mark.parametrize(
        ("submission", "expected"),
        [
            (None, False),
            (_submission(status="received"), False),
            (_submission(status="completed", review_markdown=None), False),
            (_submission(status="completed", review_markdown="  "), False),
            (_submission(status="completed"), True),
            (_submission(status="delivered"), True),
        ],
    )
    def test_readable_means_reviewed_and_written(
        self, submission: HomeworkSubmission | None, expected: bool
    ) -> None:
        """One definition, used by the server and by the portal's buttons."""
        assert feedback_core.review_is_readable(submission) is expected
