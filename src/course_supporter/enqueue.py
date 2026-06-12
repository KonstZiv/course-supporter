"""Enqueue helpers for submitting jobs to ARQ with DB tracking."""

from __future__ import annotations

import uuid

import structlog
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.job_priority import JobPriority
from course_supporter.jobs import JobType
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job


async def enqueue_ingestion(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    material_id: uuid.UUID,
    source_type: str,
    source_url: str,
    priority: JobPriority = JobPriority.NORMAL,
) -> Job:
    """Create a Job record, enqueue ingestion to ARQ, flip entry to PENDING.

    The caller is responsible for committing the session.

    Single source of truth for the RAW/READY/ERROR → PENDING transition:
    this helper is the one place where a material entry acquires a
    ``job_id`` and ``pending_since`` prior to processing. That way the
    UI sees state=pending immediately after the create/retry call returns,
    without having to wait for the worker to pick up the ARQ task. The
    worker still calls ``set_pending`` defensively on task entry — it is
    idempotent and protects against manual/out-of-band enqueues that
    bypass this helper.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        node_id: CourseNode that owns the material entry.
        material_id: AuthoredDocument to ingest.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority (NORMAL respects work window).

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(
        node_id=str(node_id), material_id=str(material_id)
    )
    job_repo = JobRepository(session)
    entry_repo = AuthoredDocumentRepository(session)

    job = await job_repo.create(
        tenant_id=tenant_id,
        course_node_id=node_id,
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
        await job_repo.set_arq_job_id(job.id, arq_job.job_id)

    # Synchronously mark the entry as PENDING so the UI reflects the
    # transition immediately upon return.
    await entry_repo.set_pending(material_id, job.id)

    log.info(
        "job_enqueued",
        job_id=str(job.id),
        material_id=str(material_id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job


async def enqueue_homework(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> Job:
    """Create a Job record and enqueue homework processing.

    Enqueues to the separate ``homework`` ARQ queue.
    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        submission_id: HomeworkSubmission to process.

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(submission_id=str(submission_id))
    repo = JobRepository(session)

    job = await repo.create(
        tenant_id=tenant_id,
        job_type="homework",
        input_params={
            "submission_id": str(submission_id),
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_process_homework",
        str(job.id),
        str(submission_id),
        _queue_name="homework",
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "homework_job_enqueued",
        job_id=str(job.id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job


async def enqueue_node_summary_regeneration(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vertex_node_id: uuid.UUID,
    force: bool = False,
    priority: JobPriority = JobPriority.NORMAL,
) -> Job:
    """Create a Job + enqueue the methodist two-pass regeneration task.

    Single source of truth for the
    ``node_summary_regeneration`` enqueue path (KD13 + Phase 3.2.4).
    Mirrors :func:`enqueue_ingestion` shape: persist the Job row +
    enqueue the ARQ task + record ``arq_job_id``. The caller commits.

    Args:
        redis: ARQ Redis pool.
        session: Active DB session (caller controls the transaction
            boundary; this helper does NOT commit).
        tenant_id: Owning tenant.
        vertex_node_id: Vertex CourseNode for the run.
        force: Persisted into ``input_params`` so reactivate replays
            the same shape. The 422 decision on
            ``uncovered_stale_node_ids`` lives in the calling route
            (``POST /nodes/{node_id}/summary/generate``) per K1
            ratify; the orchestrator + this helper are
            force-agnostic, and memo-skip on both axes is
            unconditional inside the run.
        priority: Job priority (NORMAL respects the work window).
    """
    log = structlog.get_logger().bind(vertex_node_id=str(vertex_node_id), force=force)
    repo = JobRepository(session)
    job = await repo.create(
        tenant_id=tenant_id,
        course_node_id=vertex_node_id,
        job_type=JobType.NODE_SUMMARY_REGENERATION,
        priority=priority.value,
        input_params={
            "vertex_node_id": str(vertex_node_id),
            "force": force,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_regenerate_node_summary",
        str(job.id),
        str(vertex_node_id),
        force,
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "node_summary_job_enqueued",
        job_id=str(job.id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job
