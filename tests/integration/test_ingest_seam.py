"""Live gesture: ingest skip-if-dead through the L2 seam (acceptance §3).

Soft-delete the AuthoredDocument while its ingest Job is queued → the seam turns
the Job ``obsolete`` and skips the pipeline body entirely (no LLM, no
processing, the job never even goes ``active``). Real PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import AuthoredDocument

pytestmark = pytest.mark.requires_db


async def _seed_queued_ingest_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    material_id: uuid.UUID,
) -> uuid.UUID:
    async with session_factory() as session:
        job = await JobRepository(session).create(
            tenant_id=tenant_id,
            course_node_id=node_id,
            job_type=JobType.DOCUMENT_PROCESSING,
            input_params={"material_id": str(material_id)},
            subject_type="authored_document",
            subject_id=material_id,
        )
        await session.commit()
        return job.id


class TestIngestSeamSkipIfDead:
    async def test_soft_deleted_material_obsoletes_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        mid = committed_seeds["material_id"]

        # Subject dies while the job is queued.
        async with session_factory() as session:
            await session.execute(
                update(AuthoredDocument)
                .where(AuthoredDocument.id == mid)
                .values(deleted_at=datetime.now(UTC))
            )
            await session.commit()

        job_id = await _seed_queued_ingest_job(
            session_factory,
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["course_node_id"],
            material_id=mid,
        )

        # Only session_factory is needed: the seam skips the body on a dead
        # subject, so model_router / stage_router / redis are never touched.
        ctx = {"session_factory": session_factory}
        result = await arq_ingest_material(
            ctx, str(job_id), str(mid), "web", "https://example.com/e2e"
        )
        assert result is None

        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(job_id)
            assert job is not None
            assert job.status == "obsolete"  # skip-if-dead, not failed
            assert job.completed_at is not None  # termination time stamped
            assert job.started_at is None  # body never ran → never went active
            assert job.error_message is None  # obsolete is not a failure
