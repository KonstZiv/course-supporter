"""End-to-end integration tests for arq_ingest_material with real DB.

Calls the task function directly (no real ARQ worker) with a real
session_factory and mocked processor. Verifies the full DB state
transitions for success and failure paths.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_arq_task_e2e.py --run-db -v``
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.models.source import SourceType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.repositories import SourceMaterialRepository

pytestmark = pytest.mark.requires_db

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Build a minimal ARQ worker context dict."""
    return {
        "session_factory": session_factory,
        "model_router": MagicMock(),
    }


def _mock_processors(
    *,
    content: str = '{"sections": []}',
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a mock processor instance."""
    mock_doc = MagicMock()
    mock_doc.model_dump_json.return_value = content
    processor = MagicMock()
    processor.process = AsyncMock(return_value=mock_doc)
    return {SourceType.WEB: processor}


def _failing_processors(
    *,
    error: str = "Processing failed",
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a processor that raises."""
    processor = MagicMock()
    processor.process = AsyncMock(side_effect=RuntimeError(error))
    return {SourceType.WEB: processor}


class TestArqIngestMaterialE2E:
    """End-to-end arq_ingest_material with real DB, mocked processor."""

    async def test_success_full_lifecycle(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Full success: queued->complete, pending->done, content set.

        1. Seed Job (queued) + Material (pending) in DB.
        2. Mock processor to return a valid SourceDocument.
        3. Call arq_ingest_material with real ctx.
        4. Assert DB: Job=complete, Material=done, content_snapshot set.
        """
        mid = committed_seeds["material_id"]
        cid = committed_seeds["course_id"]

        # Create job in DB
        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(course_id=cid, job_type="ingest")
            await session.commit()
            job_id = job.id

        content = '{"sections": [{"title": "E2E Test"}]}'
        ctx = _build_ctx(session_factory)

        with (
            patch(
                "course_supporter.job_priority.check_work_window",
            ),
            patch(_HEAVY),
            patch(
                _FACTORY,
                return_value=_mock_processors(content=content),
            ),
        ):
            await arq_ingest_material(
                ctx,
                str(job_id),
                str(mid),
                "web",
                "https://example.com/e2e",
            )

        # Verify final DB state
        async with session_factory() as session:
            job_repo = JobRepository(session)
            mat_repo = SourceMaterialRepository(session)

            final_job = await job_repo.get_by_id(job_id)
            final_mat = await mat_repo.get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "complete"
        assert final_job.completed_at is not None
        assert final_job.result_material_id == mid

        assert final_mat is not None
        assert final_mat.status == "done"
        assert final_mat.content_snapshot == content

    async def test_failure_full_lifecycle(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Full failure: queued->failed, pending->error, error_message.

        1. Seed Job (queued) + Material (pending) in DB.
        2. Mock processor to raise an exception.
        3. Call arq_ingest_material with real ctx.
        4. Assert DB: Job=failed, Material=error, error_message set.
        """
        mid = committed_seeds["material_id"]
        cid = committed_seeds["course_id"]

        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(course_id=cid, job_type="ingest")
            await session.commit()
            job_id = job.id

        error_msg = "E2E processor crash"
        ctx = _build_ctx(session_factory)

        with (
            patch(
                "course_supporter.job_priority.check_work_window",
            ),
            patch(_HEAVY),
            patch(
                _FACTORY,
                return_value=_failing_processors(error=error_msg),
            ),
        ):
            await arq_ingest_material(
                ctx,
                str(job_id),
                str(mid),
                "web",
                "https://example.com/e2e-fail",
            )

        # Verify final DB state
        async with session_factory() as session:
            job_repo = JobRepository(session)
            mat_repo = SourceMaterialRepository(session)

            final_job = await job_repo.get_by_id(job_id)
            final_mat = await mat_repo.get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "failed"
        assert error_msg in (final_job.error_message or "")
        assert final_job.completed_at is not None

        assert final_mat is not None
        assert final_mat.status == "error"
        assert error_msg in (final_mat.error_message or "")

    async def test_unknown_source_type_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Invalid source_type causes failure path.

        The SourceType() constructor raises ValueError for unknown types,
        triggering the on_failure callback.
        """
        mid = committed_seeds["material_id"]
        cid = committed_seeds["course_id"]

        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(course_id=cid, job_type="ingest")
            await session.commit()
            job_id = job.id

        ctx = _build_ctx(session_factory)

        with patch(
            "course_supporter.job_priority.check_work_window",
        ):
            await arq_ingest_material(
                ctx,
                str(job_id),
                str(mid),
                "nonexistent_type",
                "https://example.com/bad-type",
            )

        async with session_factory() as session:
            job_repo = JobRepository(session)
            final_job = await job_repo.get_by_id(job_id)

        assert final_job is not None
        assert final_job.status == "failed"
        assert final_job.error_message is not None
