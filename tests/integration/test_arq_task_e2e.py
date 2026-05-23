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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.models.source import SourceType
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import DocumentSegment, DocumentSummary

pytestmark = pytest.mark.requires_db

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Build a minimal ARQ worker context dict.

    ``redis`` is the ArqRedis pool that arq injects into every job ctx
    (the shared task unpacks it for the audio/video processor factory,
    task 2.4.2); mocked here since this factory is patched out.
    """
    return {
        "session_factory": session_factory,
        "model_router": MagicMock(),
        "stage_router": MagicMock(),
        "redis": MagicMock(),
    }


def _mock_processors(
    *,
    content: str = '{"sections": []}',
    detected_language: str | None = None,
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a mock processor instance.

    ``detected_language`` controls ``doc.metadata["detected_language"]``
    so callers can cover both the no-detect path and the STT auto-detect
    cache path. ``metadata`` is set to a real dict to prevent MagicMock
    auto-mocking from producing unserialisable values for downstream SQL.

    Phase 2.1 C7 — Pass 2b is exercised end-to-end: the mocked
    ``process_macro`` returns a ``DocumentSummaryDraft`` carrying segment
    drafts (with offsets that line up to the chunk text), and
    ``process_detail`` is a real ``AsyncMock`` returning the same drafts
    with ``content`` pre-filled. The integration test asserts both
    ``DocumentSummary`` and N ``DocumentSegment`` rows persist with
    cascade hash propagation reaching the root CourseNode.
    """
    from course_supporter.ingestion.schemas import (
        DocumentSegmentDraft,
        DocumentSummaryDraft,
    )
    from course_supporter.models.source import (
        ChunkType,
        ContentChunk,
        SourceDocument,
    )

    # Real SourceDocument so ``doc.assemble_text()`` /
    # ``doc.model_dump_json()`` / ``doc.metadata`` / ``doc.chunks`` all
    # behave under Pass 2b wiring (api/tasks.py calls
    # ``processor.process_detail(doc, summary_draft)`` and then
    # ``DocumentSegmentRepository.create_batch(..., source_doc=doc)``
    # which invokes ``doc.assemble_text()``). The ``content`` argument
    # is retained for backwards-compatible signature; Pydantic's own
    # JSON serialisation supplies the on_success payload.
    real_doc = SourceDocument(
        source_type=SourceType.WEB,
        source_url="https://example.com/e2e",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.WEB_CONTENT,
                text="Educational test content body one.",
                index=0,
            ),
            ContentChunk(
                chunk_type=ChunkType.WEB_CONTENT,
                text="Educational test content body two.",
                index=1,
            ),
        ],
        metadata=(
            {"detected_language": detected_language}
            if detected_language is not None
            else {}
        ),
    )
    _ = content  # parameter kept for legacy callers; serialisation is real

    # Segment drafts cover the real_doc.assemble_text() output
    # (70 chars: 34 + "\n\n" + 34) per fixup 2.1.7.2 server-side
    # coverage assertion. Content pre-filled so process_detail is a
    # straight passthrough -- this test verifies repository
    # materialisation, not slicing (that lives in unit tests).
    seg_drafts = [
        DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=36,
            title="Seg A",
            description="First test segment.",
            main_concepts=["alpha"],
            secondary_concepts=[],
            content="seg A body",
        ),
        DocumentSegmentDraft(
            order=1,
            start_pos=36,
            end_pos=70,
            title="Seg B",
            description="Second test segment.",
            main_concepts=["beta"],
            secondary_concepts=["gamma"],
            content="seg B body",
        ),
    ]

    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=real_doc)
    processor.process_macro = AsyncMock(
        return_value=DocumentSummaryDraft(
            title="t",
            description="d",
            main_concepts=["alpha", "beta"],
            secondary_concepts=["gamma"],
            # Fixup 2.1.7.2: content_char_count derived server-side
            # from doc.assemble_text() — no longer a Pydantic field on
            # the draft.
            segments=seg_drafts,
        )
    )
    processor.process_detail = AsyncMock(return_value=seg_drafts)
    return {SourceType.WEB: processor}


def _failing_processors(
    *,
    error: str = "Processing failed",
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a processor that raises."""
    processor = MagicMock()
    processor.process_raw = AsyncMock(side_effect=RuntimeError(error))
    return {SourceType.WEB: processor}


class TestArqIngestMaterialE2E:
    """End-to-end arq_ingest_material with real DB, mocked processor."""

    @pytest.mark.parametrize(
        ("detected_language", "expected_language"),
        [
            pytest.param(None, None, id="no_detection"),
            pytest.param("uk", "uk", id="with_detection_cached"),
        ],
    )
    async def test_success_full_lifecycle(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        detected_language: str | None,
        expected_language: str | None,
    ) -> None:
        """Full success: queued->complete, pending->done, content set.

        Also covers the STT auto-detect language cache: when the
        processor reports ``detected_language``, it is persisted to
        ``AuthoredDocument.language`` (entries seeded with language=NULL).
        """
        mid = committed_seeds["material_id"]
        tid = committed_seeds["tenant_id"]
        nid = committed_seeds["course_node_id"]

        # Create job in DB
        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(
                tenant_id=tid,
                course_node_id=nid,
                job_type="ingest",
            )
            await session.commit()
            job_id = job.id

        content = '{"sections": [{"title": "E2E Test"}]}'
        ctx = _build_ctx(session_factory)

        from course_supporter.security.schemas import SafetyResult

        safe_verdict = SafetyResult(
            is_safe=True,
            violations=[],
            confidence=0.95,
            reasoning="benign",
        )

        with (
            patch(
                "course_supporter.job_priority.check_work_window",
            ),
            patch(_HEAVY),
            patch(
                _FACTORY,
                return_value=_mock_processors(
                    content=content,
                    detected_language=detected_language,
                ),
            ),
            patch(
                "course_supporter.security.stage2.run_stage2_safety_check",
                new=AsyncMock(return_value=safe_verdict),
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
            mat_repo = AuthoredDocumentRepository(session)

            final_job = await job_repo.get_by_id(job_id)
            final_mat = await mat_repo.get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "complete"
        assert final_job.completed_at is not None
        assert final_mat is not None
        assert final_mat.state == "ready"
        assert final_mat.language == expected_language

        # Regression test for the Pass 2a commit gap (vision-side
        # ratified 2026-05-12): without the explicit ``session.commit()``
        # after ``DocumentSummaryRepository.create(...)`` in
        # ``arq_ingest_material``, the summary INSERT was rolled back
        # when the task's ``async with session_factory()`` block exited.
        # Verifying persistence (not a mock-spy on ``session.commit``)
        # because persistence is the actual contract the fix enforces.
        async with session_factory() as verify_session:
            result = await verify_session.execute(
                select(DocumentSummary).where(
                    DocumentSummary.authored_document_id == mid
                )
            )
            summary = result.scalar_one_or_none()
        assert summary is not None, "Pass 2a DocumentSummary did not persist"
        assert summary.title == "t"
        assert summary.description == "d"
        # Aggregated document-level concepts survive end-to-end.
        assert summary.main_concepts == ["alpha", "beta"]
        assert summary.secondary_concepts == ["gamma"]

        # Phase 2.1 C7 — Pass 2b materialised DocumentSegment rows with
        # title/description from drafts + cascade hash propagation up to
        # the root CourseNode.
        async with session_factory() as verify_session:
            seg_result = await verify_session.execute(
                select(DocumentSegment)
                .where(DocumentSegment.document_summary_id == summary.id)
                .order_by(DocumentSegment.order)
            )
            segments = list(seg_result.scalars().all())
        assert len(segments) == 2, "Pass 2b did not persist segment rows"
        assert [s.title for s in segments] == ["Seg A", "Seg B"]
        assert [s.content for s in segments] == ["seg A body", "seg B body"]
        assert all(s.content_hash is not None for s in segments)
        assert all(s.course_root_id == summary.course_root_id for s in segments)

        # KD-2.1-F cascade reaches the root: summary + AuthoredDocument
        # + CourseNode root all carry post-Pass-2b content_hash.
        async with session_factory() as verify_session:
            mat_repo = AuthoredDocumentRepository(verify_session)
            node_repo = CourseNodeRepository(verify_session)
            entry = await mat_repo.get_by_id(mid)
            assert entry is not None
            root = await node_repo.get_root_for(entry.course_node_id)
        assert summary.content_hash is not None
        assert entry.content_hash is not None
        assert root is not None
        assert root.content_hash is not None

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
        tid = committed_seeds["tenant_id"]
        nid = committed_seeds["course_node_id"]

        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(
                tenant_id=tid,
                course_node_id=nid,
                job_type="ingest",
            )
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
            mat_repo = AuthoredDocumentRepository(session)

            final_job = await job_repo.get_by_id(job_id)
            final_mat = await mat_repo.get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "failed"
        assert error_msg in (final_job.error_message or "")
        assert final_job.completed_at is not None

        assert final_mat is not None
        assert final_mat.state == "error"
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
        tid = committed_seeds["tenant_id"]
        nid = committed_seeds["course_node_id"]

        async with session_factory() as session:
            job_repo = JobRepository(session)
            job = await job_repo.create(
                tenant_id=tid,
                course_node_id=nid,
                job_type="ingest",
            )
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
