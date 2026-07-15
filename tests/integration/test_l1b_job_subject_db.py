"""Integration tests for the L1b typed job subject + idempotency index.

DB-tier live gestures (real PostgreSQL):

* Idempotency index enforces one in-flight job per subject; the DB is the
  final judge (acceptance 6b), NULL subjects never conflict (H-L1b-1), and
  terminal / soft-deleted rows do not occupy the slot (partial WHERE).
* ``get_inflight_job_for_subject`` — the app-check that yields the human 409.
* F3 regress: cancelling one document's subject spares its siblings.
* Node-deletion victim set cancels the whole subtree (document + node subjects).
* Force-supersede mechanism: cancel the in-flight subject job, then queue a
  fresh one — exactly one in-flight (acceptance 5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import CourseNode, Job, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

FIXED_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_tenant_node(session: AsyncSession) -> tuple[Tenant, CourseNode]:
    t = Tenant(name=f"l1b-{uuid.uuid4().hex[:6]}")
    session.add(t)
    await session.flush()
    n = make_root_course_node(tenant_id=t.id, title="n", order=0)
    session.add(n)
    await session.flush()
    return t, n


def _doc_job(
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    subject_id: uuid.UUID,
    *,
    status: str = "queued",
    **kw: object,
) -> Job:
    return Job(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type="document_processing",
        status=status,
        subject_type="authored_document",
        subject_id=subject_id,
        **kw,
    )


class TestIdempotencyIndex:
    """uq_jobs_subject_in_flight — the DB enforces one in-flight per subject."""

    async def test_second_in_flight_same_subject_raises(
        self, db_session: AsyncSession
    ) -> None:
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        db_session.add(_doc_job(t.id, n.id, doc, status="queued"))
        await db_session.flush()
        db_session.add(_doc_job(t.id, n.id, doc, status="active"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_terminal_jobs_do_not_conflict(
        self, db_session: AsyncSession
    ) -> None:
        """Two COMPLETE jobs on one subject coexist — index is partial on
        in-flight statuses only."""
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        db_session.add(_doc_job(t.id, n.id, doc, status="complete"))
        db_session.add(_doc_job(t.id, n.id, doc, status="complete"))
        await db_session.flush()  # must not raise

    async def test_null_subjects_do_not_conflict(
        self, db_session: AsyncSession
    ) -> None:
        """Two in-flight s3_cleanup jobs (NULL subject) coexist — the index has
        NO ``NULLS NOT DISTINCT`` (H-L1b-1: parallel cleanups must not clash)."""
        t, n = await _seed_tenant_node(db_session)
        for _ in range(2):
            db_session.add(
                Job(
                    tenant_id=t.id,
                    course_node_id=n.id,
                    job_type="s3_cleanup",
                    status="queued",
                    subject_type=None,
                    subject_id=None,
                    input_params={"file_keys": ["k"]},
                )
            )
        await db_session.flush()  # must not raise

    async def test_soft_deleted_does_not_occupy_slot(
        self, db_session: AsyncSession
    ) -> None:
        """A soft-deleted in-flight job frees the slot (partial WHERE
        deleted_at IS NULL)."""
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        db_session.add(
            _doc_job(
                t.id,
                n.id,
                doc,
                status="active",
                deleted_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await db_session.flush()
        db_session.add(_doc_job(t.id, n.id, doc, status="active"))
        await db_session.flush()  # must not raise — the soft-deleted row is excluded


class TestInflightAppCheck:
    """get_inflight_job_for_subject — the human-409 pre-check."""

    async def test_detects_existing_in_flight(self, db_session: AsyncSession) -> None:
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        job = _doc_job(t.id, n.id, doc, status="active")
        db_session.add(job)
        await db_session.flush()

        repo = JobRepository(db_session)
        found = await repo.get_inflight_job_for_subject("authored_document", doc)
        assert found is not None
        assert found.id == job.id

    async def test_none_for_other_subject(self, db_session: AsyncSession) -> None:
        t, n = await _seed_tenant_node(db_session)
        db_session.add(_doc_job(t.id, n.id, uuid.uuid4(), status="active"))
        await db_session.flush()

        repo = JobRepository(db_session)
        assert (
            await repo.get_inflight_job_for_subject("authored_document", uuid.uuid4())
            is None
        )

    async def test_none_for_null_subject(self, db_session: AsyncSession) -> None:
        repo = JobRepository(db_session)
        assert await repo.get_inflight_job_for_subject(None, None) is None

    async def test_none_when_only_terminal(self, db_session: AsyncSession) -> None:
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        db_session.add(_doc_job(t.id, n.id, doc, status="complete"))
        await db_session.flush()

        repo = JobRepository(db_session)
        assert await repo.get_inflight_job_for_subject("authored_document", doc) is None


class TestCancellationGestures:
    """Live per-subject cancellation — F3 regress + node-deletion subtree."""

    async def test_deleting_one_subject_spares_siblings(
        self, db_session: AsyncSession
    ) -> None:
        """F3 regress: two documents on one node, each with a live job. Cancel
        ONE document's subject → its job cancelled, the sibling stays active.
        (The old course_node_id arm cancelled BOTH.)"""
        t, n = await _seed_tenant_node(db_session)
        doc1, doc2 = uuid.uuid4(), uuid.uuid4()
        job1 = _doc_job(t.id, n.id, doc1, status="active")
        job2 = _doc_job(t.id, n.id, doc2, status="active")
        db_session.add_all([job1, job2])
        await db_session.flush()

        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc1], now=FIXED_TS
        )
        await db_session.refresh(job1)
        await db_session.refresh(job2)

        assert job1.status == "cancelled"
        assert job2.status == "active"  # sibling untouched

    async def test_node_deletion_victim_set_cancels_subtree(
        self, db_session: AsyncSession
    ) -> None:
        """The full victim set from a node deletion (node id + subtree document
        ids) cancels both the document-subject jobs and the node-subject job."""
        t, n = await _seed_tenant_node(db_session)
        doc1, doc2 = uuid.uuid4(), uuid.uuid4()
        doc_job1 = _doc_job(t.id, n.id, doc1, status="active")
        doc_job2 = _doc_job(t.id, n.id, doc2, status="queued")
        node_job = Job(
            tenant_id=t.id,
            course_node_id=n.id,
            job_type="node_summary_regeneration",
            status="active",
            subject_type="course_node",
            subject_id=n.id,
            input_params={"vertex_node_id": str(n.id), "force": False},
        )
        db_session.add_all([doc_job1, doc_job2, node_job])
        await db_session.flush()

        # The cascade engine hands the full mixed victim set.
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [n.id, doc1, doc2], now=FIXED_TS
        )
        for j in (doc_job1, doc_job2, node_job):
            await db_session.refresh(j)
            assert j.status == "cancelled"

    async def test_force_supersede_mechanism(self, db_session: AsyncSession) -> None:
        """Acceptance 5 mechanism: cancel the in-flight subject job, then queue
        a fresh one — the new insert is accepted and exactly one in-flight
        remains, with the old job's completed_at stamped."""
        t, n = await _seed_tenant_node(db_session)
        doc = uuid.uuid4()
        old = _doc_job(t.id, n.id, doc, status="active")
        db_session.add(old)
        await db_session.flush()

        # Supersede: cancel the subject's in-flight jobs (retry ?force step 1).
        await JobCancellationService(db_session).cancel_jobs_for_entities(
            [doc], now=FIXED_TS
        )
        # Then queue the fresh job (step 2) — must not collide (old is at rest).
        new = _doc_job(t.id, n.id, doc, status="queued")
        db_session.add(new)
        await db_session.flush()

        await db_session.refresh(old)
        assert old.status == "cancelled"
        assert old.completed_at == FIXED_TS

        repo = JobRepository(db_session)
        inflight = await repo.get_inflight_job_for_subject("authored_document", doc)
        assert inflight is not None
        assert inflight.id == new.id
