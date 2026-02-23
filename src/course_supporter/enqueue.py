"""Enqueue helpers for submitting jobs to ARQ with DB tracking."""

from __future__ import annotations

import uuid

import structlog
from arq.connections import ArqRedis
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
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "job_enqueued",
        job_id=str(job.id),
        material_id=str(material_id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job


async def enqueue_generation(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    course_id: uuid.UUID,
    node_id: uuid.UUID | None = None,
    mode: str = "free",
    depends_on: list[str] | None = None,
) -> Job:
    """Create a Job record and enqueue structure generation to ARQ.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        course_id: Course to generate structure for.
        node_id: Target node UUID (None = whole course).
        mode: Generation mode ('free' or 'guided').
        depends_on: List of Job UUIDs (str) this job depends on.

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(
        course_id=str(course_id),
        node_id=str(node_id),
    )
    repo = JobRepository(session)

    job = await repo.create(
        course_id=course_id,
        node_id=node_id,
        job_type="generate_structure",
        depends_on=depends_on,
        input_params={
            "course_id": str(course_id),
            "node_id": str(node_id) if node_id else None,
            "mode": mode,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_generate_structure",
        str(job.id),
        str(course_id),
        str(node_id) if node_id else None,
        mode,
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "generation_job_enqueued",
        job_id=str(job.id),
        mode=mode,
        depends_on=depends_on,
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job
