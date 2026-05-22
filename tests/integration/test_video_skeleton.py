"""End-to-end skeleton topology test for the video_pipeline (Phase 2.4 task 2.4.1).

Drives the real ``arq_ingest_material`` orchestrator against a live DB
with the new skeleton ``VideoProcessor`` injected via ``create_processors``.
All 7 gnízda are offline stubs (zero external calls); Stage 2 safety and
ARQ work-window are patched. The test verifies pipeline topology:

* happy path — ``source_type=video`` traverses all 7 gnízda → ``state=ready``
  with a persisted ``DocumentSummary`` + ``DocumentSegment[]`` (cascade
  content_hash to root);
* R1 failure path — a gnízdo raising ``ProcessingError`` → ``state=ERROR``
  with non-empty ``error_message``, the row **retained** (NOT soft-deleted,
  per KD-2.1-P + task 2.4.8 retry), and nothing persisted.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_video_skeleton.py --run-db -v``
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.models.source import SourceType
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
)

pytestmark = pytest.mark.requires_db

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"
_WORK_WINDOW = "course_supporter.job_priority.check_work_window"
_STAGE2 = "course_supporter.security.stage2.run_stage2_safety_check"
_STEP4 = "course_supporter.ingestion.video_pipeline.steps.step_4_pass1_vision"

_SOURCE_URL = "s3://bucket/lecture.mp4"


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Minimal ARQ worker context for the ingestion task."""
    return {
        "session_factory": session_factory,
        "model_router": MagicMock(),
        "stage_router": MagicMock(),
        "redis": MagicMock(),
        "s3_client": None,
    }


def _video_processors() -> dict[SourceType, VideoProcessor]:
    """Inject the real skeleton VideoProcessor at the VIDEO dispatch key."""
    return {SourceType.VIDEO: VideoProcessor()}


async def _seed_video_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> uuid.UUID:
    """Flip the seeded document to source_type=video and create its Job."""
    async with session_factory() as session:
        await session.execute(
            update(AuthoredDocument)
            .where(AuthoredDocument.id == committed_seeds["material_id"])
            .values(source_type="video")
        )
        job = await JobRepository(session).create(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="ingest",
        )
        await session.commit()
        return job.id


class TestVideoSkeletonTopology:
    """Acceptance #1-#2 — full orchestrator topology over the skeleton."""

    async def test_skeleton_reaches_ready_with_persisted_cascade(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Mock video matter traverses 7 gnízda → state=ready + Summary + Segments."""
        mid = committed_seeds["material_id"]
        job_id = await _seed_video_job(session_factory, committed_seeds)
        ctx = _build_ctx(session_factory)

        from course_supporter.security.schemas import SafetyResult

        safe_verdict = SafetyResult(
            is_safe=True,
            violations=[],
            confidence=0.95,
            reasoning="benign",
        )

        with (
            patch(_WORK_WINDOW),
            patch(_HEAVY),
            patch(_FACTORY, return_value=_video_processors()),
            patch(_STAGE2, new=AsyncMock(return_value=safe_verdict)),
        ):
            await arq_ingest_material(
                ctx,
                str(job_id),
                str(mid),
                "video",
                _SOURCE_URL,
            )

        # Final lifecycle state.
        async with session_factory() as session:
            final_job = await JobRepository(session).get_by_id(job_id)
            final_mat = await AuthoredDocumentRepository(session).get_by_id(mid)
        assert final_job is not None
        assert final_job.status == "complete"
        assert final_mat is not None
        assert final_mat.state == "ready"
        assert final_mat.error_message is None

        # Pass 2a — DocumentSummary persisted.
        async with session_factory() as session:
            summary = (
                await session.execute(
                    select(DocumentSummary).where(
                        DocumentSummary.authored_document_id == mid
                    )
                )
            ).scalar_one_or_none()
        assert summary is not None, "skeleton Pass 2a did not persist a summary"

        # Pass 2b/2c — DocumentSegment rows persisted with content.
        async with session_factory() as session:
            segments = list(
                (
                    await session.execute(
                        select(DocumentSegment)
                        .where(DocumentSegment.document_summary_id == summary.id)
                        .order_by(DocumentSegment.order)
                    )
                )
                .scalars()
                .all()
            )
        assert len(segments) >= 1, "skeleton did not persist segment rows"
        assert all(seg.content for seg in segments)

        # KD9 / KD-2.1-F — cascade content_hash reaches the root CourseNode.
        async with session_factory() as session:
            mat_repo = AuthoredDocumentRepository(session)
            node_repo = CourseNodeRepository(session)
            entry = await mat_repo.get_by_id(mid)
            assert entry is not None
            root = await node_repo.get_root_for(entry.course_node_id)
        assert summary.content_hash is not None
        assert entry.content_hash is not None
        assert root is not None
        assert root.content_hash is not None

    async def test_skeleton_failure_path_marks_error_without_soft_delete(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """R1 — gnízdo ProcessingError → state=ERROR, row retained, no persist."""
        mid = committed_seeds["material_id"]
        job_id = await _seed_video_job(session_factory, committed_seeds)
        ctx = _build_ctx(session_factory)

        error_msg = "injected vision failure"

        with (
            patch(_WORK_WINDOW),
            patch(_HEAVY),
            patch(_FACTORY, return_value=_video_processors()),
            patch(
                _STEP4,
                new=AsyncMock(side_effect=ProcessingError(error_msg)),
            ),
        ):
            await arq_ingest_material(
                ctx,
                str(job_id),
                str(mid),
                "video",
                _SOURCE_URL,
            )

        async with session_factory() as session:
            final_job = await JobRepository(session).get_by_id(job_id)
            final_mat = await AuthoredDocumentRepository(session).get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "failed"
        assert error_msg in (final_job.error_message or "")

        # R1 — fail_processing only: state=ERROR, error_message set, and the
        # row is RETAINED (not soft-deleted) so it stays retryable per
        # KD-2.1-P + task 2.4.8.
        assert final_mat is not None
        assert final_mat.state == "error"
        assert error_msg in (final_mat.error_message or "")
        assert final_mat.deleted_at is None

        # Failure injected in process_raw (before Pass 2a commit) → nothing persisted.
        async with session_factory() as session:
            summary = (
                await session.execute(
                    select(DocumentSummary).where(
                        DocumentSummary.authored_document_id == mid
                    )
                )
            ).scalar_one_or_none()
        assert summary is None, "no DocumentSummary should persist on failure"
