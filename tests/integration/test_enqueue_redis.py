"""Integration tests for enqueue_ingestion with real DB + Redis.

Requires ``docker compose up -d`` (PostgreSQL + Redis).
Run with: ``uv run pytest tests/integration/test_enqueue_redis.py \
--run-db --run-redis -v``
"""

from __future__ import annotations

import uuid

import pytest
from arq.connections import ArqRedis
from arq.jobs import Job as ArqJob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.enqueue import enqueue_ingestion
from course_supporter.job_priority import JobPriority
from course_supporter.storage.job_repository import JobRepository

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]


class TestEnqueueIngestion:
    """enqueue_ingestion with real DB + real Redis."""

    async def test_job_created_with_arq_job_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        arq_redis: ArqRedis,
    ) -> None:
        """After enqueue + commit, Job row has non-null arq_job_id."""
        async with session_factory() as session:
            job = await enqueue_ingestion(
                redis=arq_redis,
                session=session,
                course_id=committed_seeds["course_id"],
                material_id=committed_seeds["material_id"],
                source_type="web",
                source_url="https://example.com/test",
                priority=JobPriority.NORMAL,
            )
            await session.commit()
            job_id = job.id

        # Re-read in a fresh session to verify persistence
        async with session_factory() as session:
            repo = JobRepository(session)
            persisted = await repo.get_by_id(job_id)

        assert persisted is not None
        assert persisted.arq_job_id is not None
        assert len(persisted.arq_job_id) > 0

    async def test_arq_job_exists_in_redis(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        arq_redis: ArqRedis,
    ) -> None:
        """ARQ job is present in Redis after enqueue."""
        async with session_factory() as session:
            job = await enqueue_ingestion(
                redis=arq_redis,
                session=session,
                course_id=committed_seeds["course_id"],
                material_id=committed_seeds["material_id"],
                source_type="web",
                source_url="https://example.com/test",
                priority=JobPriority.NORMAL,
            )
            await session.commit()
            arq_job_id = job.arq_job_id

        assert arq_job_id is not None

        # Query Redis directly
        arq_job = ArqJob(arq_job_id, redis=arq_redis)
        info = await arq_job.info()
        assert info is not None
        assert info.function == "arq_ingest_material"

    async def test_enqueue_stores_input_params(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        arq_redis: ArqRedis,
    ) -> None:
        """input_params stores material_id, source_type, source_url."""
        async with session_factory() as session:
            job = await enqueue_ingestion(
                redis=arq_redis,
                session=session,
                course_id=committed_seeds["course_id"],
                material_id=committed_seeds["material_id"],
                source_type="web",
                source_url="https://example.com/input-params",
                priority=JobPriority.NORMAL,
            )
            await session.commit()
            job_id = job.id

        async with session_factory() as session:
            repo = JobRepository(session)
            persisted = await repo.get_by_id(job_id)

        assert persisted is not None
        assert persisted.input_params is not None
        assert persisted.input_params["source_type"] == "web"
        assert (
            persisted.input_params["source_url"] == "https://example.com/input-params"
        )
        assert persisted.input_params["material_id"] == str(
            committed_seeds["material_id"]
        )
