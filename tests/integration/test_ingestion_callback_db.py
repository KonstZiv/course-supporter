"""Integration tests for IngestionCallback against real PostgreSQL.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_ingestion_callback_db.py --run-db -v``

L2: IngestionCallback is domain-only — it updates the AuthoredDocument, never the
Job.status (the execution seam owns that). These tests therefore assert the
material transition AND that the seeded Job.status is left untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion_callback import IngestionCallback
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.job_repository import JobRepository

pytestmark = pytest.mark.requires_db


class TestOnSuccessDB:
    """IngestionCallback.on_success against real DB."""

    async def test_success_completes_material(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """on_success (domain-only) marks the material ready; the Job status is
        the seam's and is left as seeded (active)."""
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]

        callback = IngestionCallback(session_factory)
        await callback.on_success(job_id=jid, material_id=mid)

        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(jid)
            material = await AuthoredDocumentRepository(session).get_by_id(mid)

        assert material is not None
        assert material.state == "ready"
        # L2: the callback does not write the Job — still as seeded (active).
        assert job is not None
        assert job.status == "active"


class TestOnFailureDB:
    """IngestionCallback.on_failure against real DB."""

    async def test_failure_marks_material_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """on_failure (domain-only) marks the material error + persists the
        message; the Job status is the seam's and is left as seeded (active)."""
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]
        error_msg = "LLM provider timeout after 30s"

        callback = IngestionCallback(session_factory)
        await callback.on_failure(job_id=jid, material_id=mid, error_message=error_msg)

        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(jid)
            material = await AuthoredDocumentRepository(session).get_by_id(mid)

        assert material is not None
        assert material.state == "error"
        assert material.error_message == error_msg
        # L2: the callback does not write the Job — still as seeded (active).
        assert job is not None
        assert job.status == "active"

    async def test_failure_independent_of_rollback(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_and_material: dict[str, Any],
    ) -> None:
        """Failure callback works after a prior session rollback.

        Validates the two-session pattern: on_failure opens its own session,
        independent of any rolled-back session state.
        """
        jid = committed_job_and_material["job_id"]
        mid = committed_job_and_material["material_id"]

        # Simulate a crashed main session
        async with session_factory() as crashed_session:
            crashed_session.add_all([])  # no-op just to use the session
            await crashed_session.rollback()

        # Now call on_failure — should still work with its own session
        callback = IngestionCallback(session_factory)
        await callback.on_failure(
            job_id=jid,
            material_id=mid,
            error_message="Crashed processing session",
        )

        # The material error state was committed (independent of the rollback).
        async with session_factory() as session:
            material = await AuthoredDocumentRepository(session).get_by_id(mid)

        assert material is not None
        assert material.state == "error"
        assert material.error_message == "Crashed processing session"
