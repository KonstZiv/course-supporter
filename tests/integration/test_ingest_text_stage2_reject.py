"""Stage 2 reject path integration test (Phase 2.1 C6, KD-2.1-P).

Calls ``arq_ingest_material`` with a mocked Stage 2 verdict that
fails (``is_safe=False``) and verifies:

* ``safety_result`` JSONB column populated with ``is_safe=False`` verdict.
* AuthoredDocument soft-deleted (``deleted_at IS NOT NULL``).
* Pass 2a NOT executed (``DocumentSummary`` row absent).
* Job marked failed with ``error_category=STAGE2_REJECTED`` in logs.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_ingest_text_stage2_reject.py
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
from course_supporter.models.source import SourceType
from course_supporter.security.schemas import SafetyResult, ViolationCategory
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import AuthoredDocument, DocumentSummary

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


def _processor_macro_spy() -> tuple[dict[SourceType, MagicMock], AsyncMock]:
    """Return processors dict + the ``process_macro`` mock for spy assertions."""
    mock_doc = MagicMock()
    mock_doc.model_dump_json.return_value = '{"sections": []}'
    mock_doc.metadata = {}
    chunk = MagicMock()
    chunk.text = "Off-topic content unrelated to the course."
    mock_doc.chunks = [chunk]

    process_macro = AsyncMock()  # must NOT be awaited
    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=mock_doc)
    processor.process_macro = process_macro
    return {SourceType.WEB: processor}, process_macro


async def test_stage2_reject_soft_deletes_document_skips_pass_2a(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> None:
    """is_safe=False → soft-delete + safety_result persisted + Pass 2a skipped."""
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

    reject_verdict = SafetyResult(
        is_safe=False,
        violations=[ViolationCategory.OFF_TOPIC],
        confidence=0.88,
        reasoning="Material is off-topic relative to the active course.",
    )

    processors, process_macro_spy = _processor_macro_spy()
    ctx = _build_ctx(session_factory)
    with (
        patch("course_supporter.job_priority.check_work_window"),
        patch(_HEAVY),
        patch(_FACTORY, return_value=processors),
        patch(_STAGE2, new=AsyncMock(return_value=reject_verdict)),
    ):
        await arq_ingest_material(
            ctx,
            str(job_id),
            str(mid),
            "web",
            "https://example.com/e2e",
        )

    process_macro_spy.assert_not_awaited()

    # Verify AuthoredDocument was soft-deleted + safety_result persisted.
    # The default get_by_id filters out soft-deleted rows; query
    # the table directly so we can read both ``deleted_at`` and
    # ``safety_result`` regardless of soft-delete filtering.
    async with session_factory() as session:
        result = await session.execute(
            select(AuthoredDocument).where(AuthoredDocument.id == mid)
        )
        final_mat = result.scalar_one_or_none()

        job_repo = JobRepository(session)
        final_job = await job_repo.get_by_id(job_id)

    assert final_mat is not None
    assert final_mat.deleted_at is not None
    assert final_mat.safety_result is not None
    assert final_mat.safety_result["is_safe"] is False
    assert final_mat.safety_result["source"] == "stage2"

    assert final_job is not None
    assert final_job.status == "failed"

    # Pass 2a must NOT have written a DocumentSummary.
    async with session_factory() as verify_session:
        summary_result = await verify_session.execute(
            select(DocumentSummary).where(DocumentSummary.authored_document_id == mid)
        )
        summary = summary_result.scalar_one_or_none()
    assert summary is None
