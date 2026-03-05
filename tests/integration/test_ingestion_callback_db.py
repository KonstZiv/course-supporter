"""Integration tests for IngestionCallback against real PostgreSQL.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_ingestion_callback_db.py --run-db -v``
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion_callback import IngestionCallback
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.material_entry_repository import MaterialEntryRepository

pytestmark = pytest.mark.requires_db


class TestOnSuccessDB:
    """IngestionCallback.on_success against real DB."""

    async def test_success_updates_both(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """Both Job and MaterialEntry updated atomically.

        Pre-conditions: Job=active, MaterialEntry=pending.
        After on_success:
          - MaterialEntry.state == 'done', processed_content set
          - Job.status == 'complete'
        """
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]
        content = '{"sections": [{"title": "Test"}]}'

        callback = IngestionCallback(session_factory)
        await callback.on_success(job_id=jid, material_id=mid, content_json=content)

        # Verify in a fresh session
        async with session_factory() as session:
            job_repo = JobRepository(session)
            mat_repo = MaterialEntryRepository(session)

            job = await job_repo.get_by_id(jid)
            material = await mat_repo.get_by_id(mid)

        assert job is not None
        assert job.status == "complete"
        assert job.completed_at is not None
        assert material is not None
        assert material.state == "done"
        assert material.processed_content == content


class TestOnFailureDB:
    """IngestionCallback.on_failure against real DB."""

    async def test_failure_updates_both(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """Job -> failed, Material -> error, error_message persisted.

        Pre-conditions: Job=active, Material=processing.
        """
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]
        error_msg = "LLM provider timeout after 30s"

        callback = IngestionCallback(session_factory)
        await callback.on_failure(job_id=jid, material_id=mid, error_message=error_msg)

        async with session_factory() as session:
            job_repo = JobRepository(session)
            mat_repo = MaterialEntryRepository(session)

            job = await job_repo.get_by_id(jid)
            material = await mat_repo.get_by_id(mid)

        assert job is not None
        assert job.status == "failed"
        assert job.error_message == error_msg
        assert job.completed_at is not None

        assert material is not None
        assert material.state == "error"
        assert material.error_message == error_msg

    async def test_failure_independent_of_rollback(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """Failure callback works after a prior session rollback.

        Validates the two-session pattern: on_failure opens its own
        session, independent of any rolled-back session state.
        """
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]

        # Simulate a crashed main session
        async with session_factory() as crashed_session:
            # Do something and rollback
            crashed_session.add_all([])  # no-op just to use the session
            await crashed_session.rollback()

        # Now call on_failure — should still work with its own session
        callback = IngestionCallback(session_factory)
        await callback.on_failure(
            job_id=jid,
            material_id=mid,
            error_message="Crashed processing session",
        )

        # Verify the error state was committed
        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.get_by_id(jid)

        assert job is not None
        assert job.status == "failed"
        assert job.error_message == "Crashed processing session"
