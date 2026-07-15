"""Integration tests for JobCancellationService against real PostgreSQL.

L1b: cancellation keys on a SINGLE arm — ``Job.subject_id IN entity_ids``
(the cascade hands the full victim id set of every type). Verifies the
subject match, the in-flight status filter, the defensive
``deleted_at IS NULL`` filter, scope isolation (no over-cancel of a
different subject — the F3 fix), and idempotency. The unit-test sibling
``tests/unit/test_job_cancellation_service.py`` covers the API contract
surface (empty-input no-op, OnCancelJobs callable shape); the SQL behaviour
is exercised here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.storage.orm import CourseNode, Job, Tenant
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.job_factory import make_document_job as _doc_job

pytestmark = pytest.mark.requires_db


FIXED_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_tenant_node(session: AsyncSession) -> tuple[Tenant, CourseNode]:
    """One tenant + one root node for subject-match testing."""
    t1 = Tenant(name=f"jcs-t1-{uuid.uuid4().hex[:6]}")
    session.add(t1)
    await session.flush()
    n1 = make_root_course_node(tenant_id=t1.id, title="n1", order=0)
    session.add(n1)
    await session.flush()
    return t1, n1


class TestSubjectMatch:
    """The single subject_id arm reaches the right in-flight Jobs."""

    async def test_subject_id_match_cancels(self, db_session: AsyncSession) -> None:
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        job = _doc_job(t1.id, n1.id, doc_id, status="active")
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc_id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        assert job.completed_at == FIXED_TS

    async def test_node_summary_subject_is_the_node(
        self, db_session: AsyncSession
    ) -> None:
        """node_summary jobs have subject = the vertex CourseNode, so a node id
        in the victim set cancels them (caught on node deletion)."""
        t1, n1 = await _seed_tenant_node(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=n1.id,
            job_type="node_summary_regeneration",
            status="active",
            subject_type="course_node",
            subject_id=n1.id,
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [n1.id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        assert job.completed_at == FIXED_TS

    async def test_node_id_alone_does_not_cancel_document_job(
        self, db_session: AsyncSession
    ) -> None:
        """F3 fix: a document's job is NOT cancelled merely because the parent
        node id is in the victim set — only its own subject_id matches. The old
        ``course_node_id`` arm over-cancelled every sibling here."""
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        job = _doc_job(t1.id, n1.id, doc_id, status="active")
        db_session.add(job)
        await db_session.flush()

        # Cancel with the NODE id only — the document id is NOT in the set.
        await JobCancellationService(db_session).cancel_jobs_for_entities([n1.id])
        await db_session.refresh(job)

        assert job.status == "active"

    async def test_null_subject_job_never_matched(
        self, db_session: AsyncSession
    ) -> None:
        """An s3_cleanup-style job with NULL subject is never cancelled by
        subject match (NULL is in no id set)."""
        t1, n1 = await _seed_tenant_node(db_session)
        job = Job(
            tenant_id=t1.id,
            course_node_id=n1.id,
            job_type="s3_cleanup",
            status="queued",
            subject_type=None,
            subject_id=None,
            input_params={"file_keys": ["k1"]},
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [t1.id, n1.id]
        )
        await db_session.refresh(job)

        assert job.status == "queued"


class TestExclusions:
    """Status filter + deleted_at filter exclude correct rows."""

    async def test_complete_not_touched(self, db_session: AsyncSession) -> None:
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        job = _doc_job(t1.id, n1.id, doc_id, status="complete")
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities([doc_id])
        await db_session.refresh(job)

        assert job.status == "complete"

    async def test_already_cancelled_idempotent(self, db_session: AsyncSession) -> None:
        """Re-running over an already-cancelled Job: completed_at unchanged."""
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        original_ts = datetime(2020, 1, 1, tzinfo=UTC)
        job = _doc_job(
            t1.id, n1.id, doc_id, status="cancelled", completed_at=original_ts
        )
        db_session.add(job)
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc_id], now=FIXED_TS
        )
        await db_session.refresh(job)

        assert job.status == "cancelled"
        # Critical: NOT FIXED_TS — the row was excluded by the status filter.
        assert job.completed_at == original_ts

    async def test_soft_deleted_not_touched(self, db_session: AsyncSession) -> None:
        """Defensive deleted_at filter avoids the 0.1 trigger collision.

        Without the filter, attempting to UPDATE a soft-deleted row would be
        blocked by ``block_update_on_soft_deleted`` (the 0.1 trigger).
        """
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        job = _doc_job(
            t1.id,
            n1.id,
            doc_id,
            status="active",
            deleted_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        db_session.add(job)
        await db_session.flush()

        # Must not raise (row excluded from SELECT, no UPDATE attempted).
        await JobCancellationService(db_session).cancel_jobs_for_entities([doc_id])
        await db_session.refresh(job)

        assert job.status == "active"


class TestScopeIsolation:
    """Lookup is correctly scoped to the supplied entity_ids."""

    async def test_unrelated_subject_not_touched(
        self, db_session: AsyncSession
    ) -> None:
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        other_id = uuid.uuid4()
        job = _doc_job(t1.id, n1.id, doc_id, status="queued")
        db_session.add(job)
        await db_session.flush()

        # Cascade for an unrelated subject — this job must not be touched.
        await JobCancellationService(db_session).cancel_jobs_for_entities([other_id])
        await db_session.refresh(job)

        assert job.status == "queued"


class TestIdempotency:
    """Re-run with the same entity_ids → no-op (status filter excludes)."""

    async def test_rerun_does_not_modify_first_run_results(
        self, db_session: AsyncSession
    ) -> None:
        t1, n1 = await _seed_tenant_node(db_session)
        doc_id = uuid.uuid4()
        job = _doc_job(t1.id, n1.id, doc_id, status="queued")
        db_session.add(job)
        await db_session.flush()

        # First run cancels with FIXED_TS.
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc_id], now=FIXED_TS
        )
        # Second run with a different ts: row now 'cancelled' → excluded.
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc_id], now=datetime(2027, 2, 2, tzinfo=UTC)
        )
        await db_session.refresh(job)

        # completed_at remains the FIRST run's ts, not the second.
        assert job.completed_at == FIXED_TS
