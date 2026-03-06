"""Enqueue helpers for submitting jobs to ARQ with DB tracking."""

from __future__ import annotations

import uuid
from typing import Literal

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
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
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
        tenant_id: Owning tenant UUID.
        node_id: MaterialNode that owns the material entry.
        material_id: MaterialEntry to ingest.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority (NORMAL respects work window).

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(
        node_id=str(node_id), material_id=str(material_id)
    )
    repo = JobRepository(session)

    job = await repo.create(
        tenant_id=tenant_id,
        materialnode_id=node_id,
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
    tenant_id: uuid.UUID,
    root_node_id: uuid.UUID,
    target_node_id: uuid.UUID | None = None,
    mode: Literal["free", "guided"] = "free",
    depends_on: list[str] | None = None,  # Job UUIDs as strings
) -> Job:
    """Create a Job record and enqueue structure generation to ARQ.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        root_node_id: Root MaterialNode UUID of the tree.
        target_node_id: Target node UUID (None = whole tree).
        mode: Generation mode ('free' or 'guided').
        depends_on: List of Job UUIDs (str) this job depends on.

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    # Job.materialnode_id stores the actual target (root if whole tree)
    effective_node_id = target_node_id or root_node_id

    log = structlog.get_logger().bind(
        root_node_id=str(root_node_id),
        target_node_id=str(target_node_id),
    )
    repo = JobRepository(session)

    job = await repo.create(
        tenant_id=tenant_id,
        materialnode_id=effective_node_id,
        job_type="generate_structure",
        depends_on=depends_on,
        input_params={
            "root_node_id": str(root_node_id),
            "target_node_id": str(target_node_id) if target_node_id else None,
            "mode": mode,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_generate_structure",
        str(job.id),
        str(root_node_id),
        str(target_node_id) if target_node_id else None,
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


async def enqueue_step(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    root_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    mode: Literal["free", "guided"] = "free",
    step_type: str = "generate",
    depends_on: list[str] | None = None,
) -> Job:
    """Create a Job record and enqueue a step via arq_execute_step.

    Unlike :func:`enqueue_generation`, this always targets a specific
    node (no ``None`` for whole-tree) and routes to the generic
    Step Executor.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        root_node_id: Root MaterialNode UUID of the tree.
        target_node_id: Specific node UUID to generate for.
        mode: Generation mode ('free' or 'guided').
        step_type: Step type ('generate', 'reconcile', 'refine').
        depends_on: List of Job UUIDs (str) this job depends on.

    Returns:
        The created Job with ``arq_job_id`` set.
    """
    log = structlog.get_logger().bind(
        root_node_id=str(root_node_id),
        target_node_id=str(target_node_id),
        step_type=step_type,
    )
    repo = JobRepository(session)

    # Validate depends_on are valid UUIDs
    validated_deps: list[str] | None = None
    if depends_on:
        validated_deps = [str(uuid.UUID(dep)) for dep in depends_on]

    job = await repo.create(
        tenant_id=tenant_id,
        materialnode_id=target_node_id,
        job_type=step_type,
        depends_on=validated_deps,
        input_params={
            "root_node_id": str(root_node_id),
            "target_node_id": str(target_node_id),
            "mode": mode,
            "step_type": step_type,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_execute_step",
        str(job.id),
        str(root_node_id),
        str(target_node_id),
        mode,
        step_type,
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "step_job_enqueued",
        job_id=str(job.id),
        mode=mode,
        step_type=step_type,
        depends_on_count=len(validated_deps) if validated_deps else 0,
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job
