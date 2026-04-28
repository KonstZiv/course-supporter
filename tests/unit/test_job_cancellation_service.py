"""Unit tests for JobCancellationService input/contract surface.

SQL semantics (OR-of-paths lookup, status filter, deleted_at filter,
idempotency) are covered against real PostgreSQL in
``tests/integration/test_job_cancellation_service_db.py``. This file
locks down the API contract so cascade engine bindings stay valid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.storage.cascade import OnCancelJobs


class TestEmptyInput:
    async def test_empty_entity_ids_noop(self) -> None:
        """Empty entity_ids returns immediately without DB calls."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()

        svc = JobCancellationService(session)
        result = await svc.cancel_jobs_for_entities([])

        # Empty input must not touch the session at all
        session.execute.assert_not_called()
        session.flush.assert_not_called()
        # And must return None (not an empty list etc.)
        assert result is None


class TestSignatureContract:
    """cancel_jobs_for_entities matches the OnCancelJobs callable shape.

    Cascade engine invokes the hook with one positional ``list[UUID]``
    argument (no kwargs). The service's bound method must satisfy that
    shape so it can be passed directly as ``on_cancel_jobs=...``.
    """

    async def test_callable_with_only_positional_arg(self) -> None:
        """Bound method invocable with positional list[UUID] only."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()

        svc = JobCancellationService(session)
        # Cascade-engine-style invocation: positional list, no kwargs
        await svc.cancel_jobs_for_entities([])

    def test_bound_method_satisfies_on_cancel_jobs_protocol(self) -> None:
        """The bound method must be assignable to the OnCancelJobs alias.

        This is the binding shape Phase 1+ entity tasks use::

            cascade_service.soft_delete_with_cascade(
                ..., on_cancel_jobs=svc.cancel_jobs_for_entities,
            )

        Locking it down here prevents accidental signature drift breaking
        all future cascade wiring at once.
        """
        session = AsyncMock()
        svc = JobCancellationService(session)
        hook: OnCancelJobs = svc.cancel_jobs_for_entities
        assert callable(hook)

    async def test_now_kwarg_optional_for_test_determinism(self) -> None:
        """``now`` is keyword-only with default None; cascade never passes it.

        Test code can supply ``now`` explicitly for deterministic timestamps,
        but production cascade callers omit it.
        """
        session = AsyncMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()

        svc = JobCancellationService(session)
        # Both invocations must work identically on empty input
        await svc.cancel_jobs_for_entities([])
        await svc.cancel_jobs_for_entities([], now=datetime(2026, 1, 1, tzinfo=UTC))


class TestReturnType:
    async def test_returns_none(self) -> None:
        """Hook returns None — never a list/dict/etc.

        Locked down because cascade engine ignores the return value;
        a non-None return would suggest the API conveys data when it
        does not (the side-effect is the whole point).
        """
        session = AsyncMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()

        svc = JobCancellationService(session)
        empty_result = await svc.cancel_jobs_for_entities([])
        assert empty_result is None
