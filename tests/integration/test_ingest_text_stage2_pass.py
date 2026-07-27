"""Stage 2 happy path integration test (Phase 2.1 C6, KD-2.1-P).

Calls ``arq_ingest_material`` with a mocked Stage 2 verdict that
passes (``is_safe=True``) and verifies:

* ``safety_result`` JSONB column populated with stage2 verdict.
* Job completes successfully (status=complete).
* AuthoredDocument transitions to ready (state derivation reads
  ``job_id IS NULL`` and ``error_message IS NULL``).
* DocumentSummary persisted (Pass 2a ran post-Stage-2).
* Document NOT soft-deleted.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_ingest_text_stage2_pass.py
--run-db -v``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.models.source import SourceType
from course_supporter.security.schemas import SafetyResult
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import DocumentSummary

pytestmark = pytest.mark.requires_db

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"
_STAGE2 = "course_supporter.security.stage2.run_stage2_safety_check"


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    return {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        # ArqRedis pool injected into every job ctx; the shared task unpacks
        # it for the audio/video processor factory (task 2.4.2). Mocked here
        # since create_processors is patched out.
        "redis": MagicMock(),
    }


def _passing_processors() -> dict[SourceType, MagicMock]:
    mock_doc = MagicMock()
    mock_doc.model_dump_json.return_value = '{"sections": []}'
    mock_doc.metadata = {}
    chunk = MagicMock()
    chunk.text = "Educational text about Python generators."
    mock_doc.chunks = [chunk]

    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=mock_doc)
    processor.process_macro = AsyncMock(
        return_value=DocumentSummaryDraft(
            title="Generators",
            description="Primer on Python generators",
            main_concepts=["generator", "yield"],
            secondary_concepts=["iterator"],
        )
    )
    # Phase 2.1 C7: Pass 2b is now wired into the worker task. Empty
    # drafts keep this test focused on Stage 2 verdict persistence
    # without materialising segment rows.
    processor.process_detail = AsyncMock(return_value=[])
    return {SourceType.WEB: processor}


async def test_stage2_pass_persists_verdict_and_runs_pass_2a(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> None:
    """is_safe=True → safety_result row populated; Pass 2a writes summary."""
    mid = committed_seeds["material_id"]
    tid = committed_seeds["tenant_id"]
    nid = committed_seeds["course_node_id"]

    async with session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
        )
        await session.commit()
        job_id = job.id

    safe_verdict = SafetyResult(
        is_safe=True,
        violations=[],
        confidence=0.92,
        reasoning="Material aligned with course topic.",
    )

    ctx = _build_ctx(session_factory)
    with (
        patch("course_supporter.job_priority.check_work_window"),
        patch(_HEAVY),
        patch(_FACTORY, return_value=_passing_processors()),
        patch(_STAGE2, new=AsyncMock(return_value=safe_verdict)),
    ):
        await arq_ingest_material(
            ctx,
            str(job_id),
            str(mid),
            "web",
            "https://example.com/e2e",
        )

    async with session_factory() as session:
        job_repo = JobRepository(session)
        mat_repo = AuthoredDocumentRepository(session)

        final_job = await job_repo.get_by_id(job_id)
        final_mat = await mat_repo.get_by_id(mid)

    assert final_job is not None
    assert final_job.status == "complete"
    assert final_mat is not None
    assert final_mat.state == "ready"
    assert final_mat.deleted_at is None
    assert final_mat.safety_result is not None
    assert final_mat.safety_result["is_safe"] is True
    assert final_mat.safety_result["source"] == "stage2"
    assert final_mat.safety_result["confidence"] == 0.92

    async with session_factory() as verify_session:
        result = await verify_session.execute(
            select(DocumentSummary).where(DocumentSummary.authored_document_id == mid)
        )
        summary = result.scalar_one_or_none()
    assert summary is not None
    assert summary.title == "Generators"
