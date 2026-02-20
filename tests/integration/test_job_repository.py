"""Integration tests for JobRepository against real PostgreSQL.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_job_repository.py --run-db -v``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Course, SourceMaterial, Tenant

pytestmark = pytest.mark.requires_db


class TestJobCreate:
    """Job creation against real PostgreSQL."""

    async def test_create_generates_uuidv7(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """Job.id is a valid UUIDv7 (version 7)."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        assert job.id is not None
        assert job.id.version == 7

    async def test_create_stores_jsonb_input_params(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """input_params dict round-trips through JSONB correctly."""
        params = {
            "material_id": str(uuid.uuid4()),
            "source_type": "web",
            "source_url": "https://example.com",
        }
        repo = JobRepository(db_session)
        job = await repo.create(
            course_id=seed_course.id,
            job_type="ingest",
            input_params=params,
        )

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.input_params == params

    async def test_create_server_default_queued_at(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """queued_at is set by server_default=func.now()."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        # Force reload from DB to see server_default
        await db_session.refresh(job, attribute_names=["queued_at"])

        assert job.queued_at is not None
        # Should be within 5 seconds of now
        delta = abs((datetime.now(UTC) - job.queued_at).total_seconds())
        assert delta < 5


class TestJobLifecycleSuccess:
    """Full success lifecycle: queued -> active -> complete."""

    async def test_full_success_lifecycle(
        self,
        db_session: AsyncSession,
        seed_course: Course,
        seed_material: SourceMaterial,
    ) -> None:
        """queued -> active -> complete with all timestamps and result."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")
        assert job.status == "queued"
        assert job.started_at is None
        assert job.completed_at is None

        # queued -> active
        active_job = await repo.update_status(job.id, "active")
        assert active_job.status == "active"
        assert active_job.started_at is not None

        # active -> complete
        complete_job = await repo.update_status(
            job.id, "complete", result_material_id=seed_material.id
        )
        assert complete_job.status == "complete"
        assert complete_job.completed_at is not None
        assert complete_job.result_material_id == seed_material.id

    async def test_active_sets_started_at(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """Transition to active populates started_at timestamp."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        active_job = await repo.update_status(job.id, "active")

        assert active_job.started_at is not None
        delta = abs((datetime.now(UTC) - active_job.started_at).total_seconds())
        assert delta < 5


class TestJobLifecycleFailureRetry:
    """Failure + retry lifecycle."""

    async def test_full_failure_retry_lifecycle(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """queued -> active -> failed -> queued (retry)."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        # queued -> active
        await repo.update_status(job.id, "active")

        # active -> failed
        failed_job = await repo.update_status(
            job.id, "failed", error_message="Timeout connecting to LLM"
        )
        assert failed_job.status == "failed"
        assert failed_job.error_message == "Timeout connecting to LLM"
        assert failed_job.completed_at is not None

        # failed -> queued (retry)
        retried_job = await repo.update_status(job.id, "queued")
        assert retried_job.status == "queued"


class TestJobTransitionValidation:
    """Invalid transitions enforced by repository."""

    async def test_invalid_transition_raises(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """queued -> complete raises ValueError (must go through active)."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        with pytest.raises(ValueError, match="Invalid job status transition"):
            await repo.update_status(job.id, "complete")

    async def test_check_constraint_both_results(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """DB CHECK prevents both result_material_id AND result_snapshot_id."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        # Bypass app-level validation via raw SQL
        with pytest.raises(IntegrityError, match="chk_job_result_exclusive"):
            await db_session.execute(
                text("""
                    UPDATE jobs
                    SET result_material_id = :mid, result_snapshot_id = :sid
                    WHERE id = :jid
                """),
                {
                    "mid": uuid.uuid4(),
                    "sid": uuid.uuid4(),
                    "jid": job.id,
                },
            )
            await db_session.flush()


class TestJobQueries:
    """Query methods against real data."""

    async def test_get_by_id_for_tenant_correct(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_course: Course,
    ) -> None:
        """Returns job when tenant matches via Course JOIN."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        found = await repo.get_by_id_for_tenant(job.id, seed_tenant.id)
        assert found is not None
        assert found.id == job.id

    async def test_get_by_id_for_tenant_wrong_tenant(
        self,
        db_session: AsyncSession,
        seed_course: Course,
    ) -> None:
        """Returns None when tenant_id does not match."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")

        wrong_tenant_id = uuid.uuid4()
        found = await repo.get_by_id_for_tenant(job.id, wrong_tenant_id)
        assert found is None

    async def test_get_active_for_course(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """Returns only queued/active jobs, ordered by queued_at."""
        repo = JobRepository(db_session)

        # Create jobs in different statuses
        job_queued = await repo.create(course_id=seed_course.id, job_type="ingest")
        job_active = await repo.create(course_id=seed_course.id, job_type="ingest")
        await repo.update_status(job_active.id, "active")

        job_complete = await repo.create(course_id=seed_course.id, job_type="ingest")
        await repo.update_status(job_complete.id, "active")
        await repo.update_status(job_complete.id, "complete")

        active_jobs = await repo.get_active_for_course(seed_course.id)
        active_ids = [j.id for j in active_jobs]

        assert job_queued.id in active_ids
        assert job_active.id in active_ids
        assert job_complete.id not in active_ids

    async def test_count_pending(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """count_pending returns accurate count with mixed statuses."""
        repo = JobRepository(db_session)

        # Count before
        initial_count = await repo.count_pending()

        # Add 3 queued
        for _ in range(3):
            await repo.create(course_id=seed_course.id, job_type="ingest")

        # Add 1 active (not counted)
        job_active = await repo.create(course_id=seed_course.id, job_type="ingest")
        await repo.update_status(job_active.id, "active")

        count = await repo.count_pending()
        assert count == initial_count + 3

    async def test_set_arq_job_id_persists(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """set_arq_job_id updates and the value is readable after flush."""
        repo = JobRepository(db_session)
        job = await repo.create(course_id=seed_course.id, job_type="ingest")
        assert job.arq_job_id is None

        await repo.set_arq_job_id(job.id, "arq:test:abc123")

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.arq_job_id == "arq:test:abc123"


class TestGetForCourse:
    """get_for_course filtering and ordering."""

    async def test_get_for_course_filters(
        self, db_session: AsyncSession, seed_course: Course
    ) -> None:
        """Filters by status and job_type, ordered by queued_at desc."""
        repo = JobRepository(db_session)

        # Create jobs of different types
        j1 = await repo.create(course_id=seed_course.id, job_type="ingest")
        j2 = await repo.create(course_id=seed_course.id, job_type="structure")
        j3 = await repo.create(course_id=seed_course.id, job_type="ingest")
        await repo.update_status(j3.id, "active")

        # Filter by job_type=ingest
        ingest_jobs = await repo.get_for_course(seed_course.id, job_type="ingest")
        ingest_ids = [j.id for j in ingest_jobs]
        assert j1.id in ingest_ids
        assert j3.id in ingest_ids
        assert j2.id not in ingest_ids

        # Filter by status=queued
        queued_jobs = await repo.get_for_course(seed_course.id, status="queued")
        queued_ids = [j.id for j in queued_jobs]
        assert j1.id in queued_ids
        assert j2.id in queued_ids
        assert j3.id not in queued_ids  # active now

        # Ordering: desc by queued_at — verify all 3 present
        all_jobs = await repo.get_for_course(seed_course.id)
        all_ids = [j.id for j in all_jobs]
        assert j1.id in all_ids
        assert j2.id in all_ids
        assert j3.id in all_ids
        # Timestamps should be non-increasing (desc order)
        timestamps = [j.queued_at for j in all_jobs]
        assert timestamps == sorted(timestamps, reverse=True)
