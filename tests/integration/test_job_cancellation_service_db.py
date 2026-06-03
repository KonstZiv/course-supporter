"""Integration tests for JobCancellationService against real PostgreSQL.

Verifies the OR-of-paths SELECT, status filter (queued/running/active),
defensive deleted_at IS NULL filter, scope isolation, and idempotency
that JobCancellationService relies on for the on_cancel_jobs cascade
hook (vision §3 KD13 + KD12). The unit-test sibling
``tests/unit/test_job_cancellation_service.py`` covers only the API
contract surface (empty-input no-op, OnCancelJobs callable shape,
return-None contract); the SQL behaviour is exercised here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.storage.orm import CourseNode, Job, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


FIXED_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_two_tenants_two_nodes(
    session: AsyncSession,
) -> tuple[Tenant, Tenant, CourseNode, CourseNode]:
    """Two tenants x two nodes for OR-of-paths + isolation testing."""
    t1 = Tenant(name=f"jcs-t1-{uuid.uuid4().hex[:6]}")
    t2 = Tenant(name=f"jcs-t2-{uuid.uuid4().hex[:6]}")
    session.add_all([t1, t2])
    await session.flush()
    n1 = make_root_course_node(tenant_id=t1.id, title="n1", order=0)
    n2 = make_root_course_node(tenant_id=t2.id, title="n2", order=0)
    session.add_all([n1, n2])
    await session.flush()
    return t1, t2, n1, n2


class TestLookupPaths:
    """All three OR-of-paths reach matching active Jobs."""

    async def test_tenant_id_match_cancels(self, db_session: AsyncSession) -> None:
        t1, _, _, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=None,
            job_type="ingest",
            status="queued",
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [t1.id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        assert job.completed_at == FIXED_TS

    async def test_course_node_id_match_cancels(self, db_session: AsyncSession) -> None:
        _, t2, n1, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t2.id,
            course_node_id=n1.id,
            job_type="ingest",
            status="running",
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [n1.id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        assert job.completed_at == FIXED_TS

    async def test_input_params_jsonb_match_cancels(
        self, db_session: AsyncSession
    ) -> None:
        """Jobs whose input_params @> {'course_node_id': <id>} also cancel.

        Covers legacy callsites where Job.course_node_id FK is NULL but
        the JSONB carries the entity reference. Status here is the legacy
        'active' alias (still in scope per pre-flight scope agreement).
        """
        _, t2, n1, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t2.id,
            course_node_id=None,
            job_type="ingest",
            status="active",
            input_params={"course_node_id": str(n1.id), "extra": "x"},
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [n1.id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        assert job.completed_at == FIXED_TS


class TestExclusions:
    """Status filter + deleted_at filter exclude correct rows."""

    async def test_completed_not_touched(self, db_session: AsyncSession) -> None:
        t1, _, _, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=None,
            job_type="ingest",
            status="completed",
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities([t1.id])
        await db_session.refresh(job)

        assert job.status == "completed"

    async def test_already_cancelled_idempotent(self, db_session: AsyncSession) -> None:
        """Re-running over an already-cancelled Job: completed_at unchanged."""
        t1, _, _, _ = await _seed_two_tenants_two_nodes(db_session)
        original_ts = datetime(2020, 1, 1, tzinfo=UTC)
        job = Job(
            tenant_id=t1.id,
            course_node_id=None,
            job_type="ingest",
            status="cancelled",
            completed_at=original_ts,
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [t1.id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        # Critical: NOT FIXED_TS — the row was excluded by the status filter
        assert job.completed_at == original_ts

    async def test_soft_deleted_not_touched(self, db_session: AsyncSession) -> None:
        """Defensive deleted_at filter avoids 0.1 trigger collision.

        Without the filter, attempting to UPDATE a soft-deleted row would
        be blocked by ``block_update_on_soft_deleted`` (the 0.1 trigger)
        because we'd try to mutate ``status``/``completed_at`` while
        ``deleted_at IS NOT NULL``.
        """
        t1, _, _, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=None,
            job_type="ingest",
            status="active",
            deleted_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        db_session.add(job)
        await db_session.flush()

        # Must not raise (row excluded from SELECT, no UPDATE attempted)
        await JobCancellationService(db_session).cancel_jobs_for_entities([t1.id])
        await db_session.refresh(job)

        assert job.status == "active"


class TestScopeIsolation:
    """Lookup is correctly scoped to the supplied entity_ids."""

    async def test_unrelated_tenant_node_not_touched(
        self, db_session: AsyncSession
    ) -> None:
        t1, t2, _, n2 = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t2.id,
            course_node_id=n2.id,
            job_type="ingest",
            status="queued",
        )
        db_session.add(job)
        await db_session.flush()

        # Cascade for t1 only — t2/n2 must not be touched
        await JobCancellationService(db_session).cancel_jobs_for_entities([t1.id])
        await db_session.refresh(job)

        assert job.status == "queued"


class TestIdempotency:
    """Re-run with the same entity_ids → no-op (status filter excludes)."""

    async def test_rerun_does_not_modify_first_run_results(
        self, db_session: AsyncSession
    ) -> None:
        t1, _, _, _ = await _seed_two_tenants_two_nodes(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=None,
            job_type="ingest",
            status="queued",
        )
        db_session.add(job)
        await db_session.flush()

        # First run cancels with FIXED_TS
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [t1.id], now=FIXED_TS
        )
        # Second run with a different ts: row now 'cancelled' → excluded
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [t1.id], now=datetime(2027, 2, 2, tzinfo=UTC)
        )
        await db_session.refresh(job)

        # completed_at remains the FIRST run's ts, not the second
        assert job.completed_at == FIXED_TS
