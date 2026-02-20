"""Enqueue helpers for submitting jobs to ARQ with DB tracking."""

from __future__ import annotations

import uuid

import structlog
from arq.connections import ArqRedis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.job_priority import JobPriority
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job


async def enqueue_ingestion(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    course_id: uuid.UUID,
    material_id: uuid.UUID,
    source_type: str,
    source_url: str,
    priority: JobPriority = JobPriority.NORMAL,
) -> Job:
    """Create a Job record and enqueue ingestion to ARQ.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        course_id: Course owning the material.
        material_id: SourceMaterial to ingest.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority (NORMAL respects work window).

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(
        course_id=str(course_id), material_id=str(material_id)
    )
    repo = JobRepository(session)

    job = await repo.create(
        course_id=course_id,
        job_type="ingest",
        priority=priority.value,
        input_params={
            "material_id": str(material_id),
            "source_type": source_type,
            "source_url": source_url,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_ingest_material",
        str(job.id),
        str(material_id),
        source_type,
        source_url,
        priority.value,
    )

    if arq_job is not None:
        stmt = update(Job).where(Job.id == job.id).values(arq_job_id=arq_job.job_id)
        await session.execute(stmt)
        await session.flush()

    log.info(
        "job_enqueued",
        job_id=str(job.id),
        material_id=str(material_id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job
