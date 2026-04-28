"""Unit tests for JobCancellationChecker (vision §3 KD13)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.jobs.cancellation import (
    JobCancellationChecker,
    JobCancelledError,
)


def _make_session_returning(status: str | None) -> AsyncMock:
    """Build an AsyncSession mock whose execute().scalar_one_or_none() == status."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=status)
    session.execute = AsyncMock(return_value=result)
    return session


class TestIsCancelled:
    """JobCancellationChecker.is_cancelled returns True for cancelled or missing."""

    async def test_queued_returns_false(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("queued"))
        assert await chk.is_cancelled(uuid.uuid4()) is False

    async def test_running_returns_false(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("running"))
        assert await chk.is_cancelled(uuid.uuid4()) is False

    async def test_completed_returns_false(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("completed"))
        assert await chk.is_cancelled(uuid.uuid4()) is False

    async def test_cancelled_returns_true(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("cancelled"))
        assert await chk.is_cancelled(uuid.uuid4()) is True

    async def test_missing_row_returns_true(self) -> None:
        """Missing Job (scalar_one_or_none → None) treated as cancelled."""
        chk = JobCancellationChecker(_make_session_returning(None))
        assert await chk.is_cancelled(uuid.uuid4()) is True


class TestRaiseIfCancelled:
    """raise_if_cancelled raises JobCancelledError with reason field."""

    async def test_queued_does_not_raise(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("queued"))
        await chk.raise_if_cancelled(uuid.uuid4())  # no-op

    async def test_running_does_not_raise(self) -> None:
        chk = JobCancellationChecker(_make_session_returning("running"))
        await chk.raise_if_cancelled(uuid.uuid4())  # no-op

    async def test_cancelled_raises_with_status_reason(self) -> None:
        job_id = uuid.uuid4()
        chk = JobCancellationChecker(_make_session_returning("cancelled"))
        with pytest.raises(JobCancelledError) as exc_info:
            await chk.raise_if_cancelled(job_id)
        assert exc_info.value.job_id == job_id
        assert exc_info.value.reason == "status=cancelled"

    async def test_missing_raises_with_not_found_reason(self) -> None:
        job_id = uuid.uuid4()
        chk = JobCancellationChecker(_make_session_returning(None))
        with pytest.raises(JobCancelledError) as exc_info:
            await chk.raise_if_cancelled(job_id)
        assert exc_info.value.job_id == job_id
        assert exc_info.value.reason == "row not found"


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
