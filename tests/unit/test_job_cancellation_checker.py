"""Unit tests for JobCancellationChecker (vision §3 KD13)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.jobs.cancellation import (
    JobCancellationChecker,
    JobCancelledError,
)
from course_supporter.storage.job_repository import JOB_TRANSITIONS

# Parametrise over EVERY real Job status (the machine keys) plus the
# missing-row sentinel (None), instead of a hand-listed table. The dead
# ``running`` token survived for years precisely because its test *looked*
# exhaustive (queued/running/completed/cancelled) while omitting the live
# statuses and inventing two that never existed. Deriving the cases from
# ``JOB_TRANSITIONS`` means a status added by a later step (e.g. L2's
# "subject vanished" terminal) is auto-covered without touching this file.
_STATUSES: list[str | None] = [*JOB_TRANSITIONS, None]


def _expected_cancelled(status: str | None) -> bool:
    """The checker's rule: exactly ``cancelled`` and a missing row (None)."""
    return status is None or status == "cancelled"


def _make_session_returning(status: str | None) -> AsyncMock:
    """Build an AsyncSession mock whose execute().scalar_one_or_none() == status."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=status)
    session.execute = AsyncMock(return_value=result)
    return session


class TestIsCancelled:
    """is_cancelled is True for exactly ``cancelled`` and a missing row."""

    @pytest.mark.parametrize("status", _STATUSES)
    async def test_matches_rule(self, status: str | None) -> None:
        chk = JobCancellationChecker(_make_session_returning(status))
        assert await chk.is_cancelled(uuid.uuid4()) is _expected_cancelled(status)


class TestRaiseIfCancelled:
    """raise_if_cancelled raises (with the right reason) iff cancel-equivalent."""

    @pytest.mark.parametrize("status", _STATUSES)
    async def test_matches_rule(self, status: str | None) -> None:
        job_id = uuid.uuid4()
        chk = JobCancellationChecker(_make_session_returning(status))
        if _expected_cancelled(status):
            with pytest.raises(JobCancelledError) as exc_info:
                await chk.raise_if_cancelled(job_id)
            assert exc_info.value.job_id == job_id
            expected_reason = "row not found" if status is None else "status=cancelled"
            assert exc_info.value.reason == expected_reason
        else:
            await chk.raise_if_cancelled(job_id)  # no-op


class TestJobCancelledError:
    """JobCancelledError carries job_id + reason and formats nicely."""

    def test_attributes_accessible(self) -> None:
        jid = uuid.uuid4()
        err = JobCancelledError(jid, "status=cancelled")
        assert err.job_id == jid
        assert err.reason == "status=cancelled"

    def test_str_includes_id_and_reason(self) -> None:
        jid = uuid.uuid4()
        err = JobCancelledError(jid, "row not found")
        assert str(jid) in str(err)
        assert "row not found" in str(err)

    def test_subclasses_exception(self) -> None:
        """Honest signal type — workers can catch with except JobCancelledError.

        Specifically NOT BaseException (asyncio.CancelledError model);
        JobCancelledError should be catchable by user-friendly except patterns.
        """
        assert issubclass(JobCancelledError, Exception)
