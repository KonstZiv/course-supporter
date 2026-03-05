"""Integration tests for JobRepository against real PostgreSQL.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_job_repository.py --run-db -v``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import MaterialEntry, MaterialNode, Tenant

pytestmark = pytest.mark.requires_db


class TestJobCreate:
    """Job creation against real PostgreSQL."""

    async def test_create_generates_uuidv7(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """Job.id is a valid UUIDv7 (version 7)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

        assert job.id is not None
        assert job.id.version == 7

    async def test_create_stores_jsonb_input_params(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """input_params dict round-trips through JSONB correctly."""
        params = {
            "material_id": str(uuid.uuid4()),
            "source_type": "web",
            "source_url": "https://example.com",
        }
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
            input_params=params,
        )

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.input_params == params

    async def test_create_server_default_queued_at(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """queued_at is set by server_default=func.now()."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

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
        seed_root_node: MaterialNode,
        seed_material_entry: MaterialEntry,
    ) -> None:
        """queued -> active -> complete with all timestamps and result."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )
        assert job.status == "queued"
        assert job.started_at is None
        assert job.completed_at is None

        # queued -> active
        active_job = await repo.update_status(job.id, "active")
        assert active_job.status == "active"
        assert active_job.started_at is not None

        # active -> complete
        complete_job = await repo.update_status(job.id, "complete")
        assert complete_job.status == "complete"
        assert complete_job.completed_at is not None

    async def test_active_sets_started_at(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """Transition to active populates started_at timestamp."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

        active_job = await repo.update_status(job.id, "active")

        assert active_job.started_at is not None
        delta = abs((datetime.now(UTC) - active_job.started_at).total_seconds())
        assert delta < 5


class TestJobLifecycleFailureRetry:
    """Failure + retry lifecycle."""

    async def test_full_failure_retry_lifecycle(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """queued -> active -> failed -> queued (retry)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

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
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """queued -> complete raises ValueError (must go through active)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

        with pytest.raises(ValueError, match="Invalid job status transition"):
            await repo.update_status(job.id, "complete")


class TestJobQueries:
    """Query methods against real data."""

    async def test_get_by_id_for_tenant_correct(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: MaterialNode,
    ) -> None:
        """Returns job when tenant matches."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

        found = await repo.get_by_id_for_tenant(job.id, seed_tenant.id)
        assert found is not None
        assert found.id == job.id

    async def test_get_by_id_for_tenant_wrong_tenant(
        self,
        db_session: AsyncSession,
        seed_root_node: MaterialNode,
    ) -> None:
        """Returns None when tenant_id does not match."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )

        wrong_tenant_id = uuid.uuid4()
        found = await repo.get_by_id_for_tenant(job.id, wrong_tenant_id)
        assert found is None

    async def test_count_pending(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """count_pending returns accurate count with mixed statuses."""
        repo = JobRepository(db_session)

        # Count before
        initial_count = await repo.count_pending()

        # Add 3 queued
        for _ in range(3):
            await repo.create(
                tenant_id=seed_root_node.tenant_id,
                node_id=seed_root_node.id,
                job_type="ingest",
            )

        # Add 1 active (not counted)
        job_active = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )
        await repo.update_status(job_active.id, "active")

        count = await repo.count_pending()
        assert count == initial_count + 3

    async def test_set_arq_job_id_persists(
        self, db_session: AsyncSession, seed_root_node: MaterialNode
    ) -> None:
        """set_arq_job_id updates and the value is readable after flush."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            node_id=seed_root_node.id,
            job_type="ingest",
        )
        assert job.arq_job_id is None

        await repo.set_arq_job_id(job.id, "arq:test:abc123")

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.arq_job_id == "arq:test:abc123"
