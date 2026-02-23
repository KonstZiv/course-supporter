"""Cascade generation orchestrator.

Detects stale materials, enqueues ingestion for them, then enqueues
structure generation with ``depends_on`` linking to ingestion jobs.
If all materials are READY, performs idempotency check before
enqueuing generation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from course_supporter.storage.orm import Job, MaterialEntry, MaterialNode


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """Result of cascade generation orchestration.

    Attributes:
        ingestion_jobs: Jobs created for stale material ingestion.
        generation_job: Generation job (None if idempotent hit).
        existing_snapshot_id: Existing snapshot UUID if idempotent.
        is_idempotent: True when no new work is needed.
    """

    ingestion_jobs: list[Job] = field(default_factory=list)
    generation_job: Job | None = None
    existing_snapshot_id: uuid.UUID | None = None
    is_idempotent: bool = False


def _partition_entries(
    flat_nodes: list[MaterialNode],
) -> tuple[list[MaterialEntry], list[MaterialEntry]]:
    """Split all entries into (stale, ready) based on MaterialState.

    PENDING entries are counted as stale (ingestion in-flight).

    Args:
        flat_nodes: Flat list of nodes with materials loaded.

    Returns:
        Tuple of (stale_entries, ready_entries).
    """
    from course_supporter.storage.orm import MaterialState

    stale: list[MaterialEntry] = []
    ready: list[MaterialEntry] = []
    for node in flat_nodes:
        for entry in node.materials:
            if entry.state == MaterialState.READY:
                ready.append(entry)
            else:
                stale.append(entry)
    return stale, ready


async def _collect_pending_job_ids(
    stale: list[MaterialEntry],
    session: AsyncSession,
) -> list[str]:
    """Collect Job UUIDs (as str) for PENDING entries.

    PENDING entries already have an in-flight ingestion job.
    We need their Job IDs for the generation ``depends_on`` list.

    Args:
        stale: Stale entries (may include PENDING ones).
        session: DB session for lookups.

    Returns:
        List of Job UUID strings for entries with pending_job_id.
    """
    return [
        str(entry.pending_job_id) for entry in stale if entry.pending_job_id is not None
    ]


async def trigger_generation(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    course_id: uuid.UUID,
    node_id: uuid.UUID | None = None,
    mode: str = "free",
) -> GenerationPlan:
    """Orchestrate cascade generation for a course or subtree.

    Detects stale materials, enqueues ingestion for non-PENDING ones,
    then enqueues structure generation with ``depends_on``. If all
    materials are READY, checks fingerprint for idempotency.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        course_id: Course UUID.
        node_id: Target node UUID (None = whole course).
        mode: Generation mode ('free' or 'guided').

    Returns:
        GenerationPlan describing the enqueued work.

    Raises:
        NodeNotFoundError: If node_id is given but not found.
        GenerationConflictError: If an active generation overlaps.
        NoReadyMaterialsError: If subtree has no materials at all.
    """
    from course_supporter.api.tasks import _resolve_target_nodes
    from course_supporter.conflict_detection import detect_conflict
    from course_supporter.enqueue import enqueue_generation, enqueue_ingestion
    from course_supporter.errors import (
        GenerationConflictError,
        NoReadyMaterialsError,
    )
    from course_supporter.fingerprint import FingerprintService
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )
    from course_supporter.storage.snapshot_repository import SnapshotRepository

    log = structlog.get_logger().bind(
        course_id=str(course_id),
        node_id=str(node_id),
        mode=mode,
    )
    log.info("trigger_generation_started")

    # 1. Load tree and resolve target
    node_repo = MaterialNodeRepository(session)
    root_nodes: list[MaterialNode] = await node_repo.get_tree(
        course_id,
        include_materials=True,
    )
    target, flat_nodes = _resolve_target_nodes(root_nodes, course_id, node_id)

    # 2. Conflict detection
    job_repo = JobRepository(session)
    active_gen_jobs = [
        j
        for j in await job_repo.get_active_for_course(course_id)
        if j.job_type == "generate_structure"
    ]
    conflict = await detect_conflict(
        session,
        course_id,
        node_id,
        active_gen_jobs,
    )
    if conflict is not None:
        raise GenerationConflictError(conflict)

    # 3. Partition entries
    stale, ready = _partition_entries(flat_nodes)

    if not stale and not ready:
        msg = "No materials found in target subtree"
        raise NoReadyMaterialsError(msg)

    # 4. If stale materials exist → cascade ingestion + generation
    if stale:
        ingestion_jobs: list[Job] = []
        depends_on_ids: list[str] = []

        # Collect existing pending job IDs (skip re-enqueue)
        pending_ids = await _collect_pending_job_ids(stale, session)
        depends_on_ids.extend(pending_ids)

        # Enqueue ingestion for non-PENDING stale entries
        for entry in stale:
            if entry.pending_job_id is not None:
                continue  # already has an in-flight job
            job = await enqueue_ingestion(
                redis=redis,
                session=session,
                course_id=course_id,
                material_id=entry.id,
                source_type=entry.source_type,
                source_url=entry.source_url,
            )
            ingestion_jobs.append(job)
            depends_on_ids.append(str(job.id))

        # Enqueue generation with depends_on
        gen_job = await enqueue_generation(
            redis=redis,
            session=session,
            course_id=course_id,
            node_id=node_id,
            mode=mode,
            depends_on=depends_on_ids if depends_on_ids else None,
        )

        log.info(
            "trigger_generation_cascade",
            ingestion_count=len(ingestion_jobs),
            pending_count=len(pending_ids),
            generation_job_id=str(gen_job.id),
        )
        return GenerationPlan(
            ingestion_jobs=ingestion_jobs,
            generation_job=gen_job,
        )

    # 5. All READY → check idempotency via fingerprint
    fp_service = FingerprintService(session)
    if target is not None:
        fingerprint = await fp_service.ensure_node_fp(target)
    else:
        fingerprint = await fp_service.ensure_course_fp(root_nodes)

    snap_repo = SnapshotRepository(session)
    existing = await snap_repo.find_by_identity(
        course_id=course_id,
        node_id=node_id,
        node_fingerprint=fingerprint,
        mode=mode,
    )
    if existing is not None:
        log.info(
            "trigger_generation_idempotent",
            snapshot_id=str(existing.id),
        )
        return GenerationPlan(
            existing_snapshot_id=existing.id,
            is_idempotent=True,
        )

    # 6. Enqueue generation (all READY, no existing snapshot)
    gen_job = await enqueue_generation(
        redis=redis,
        session=session,
        course_id=course_id,
        node_id=node_id,
        mode=mode,
    )
    log.info(
        "trigger_generation_enqueued",
        generation_job_id=str(gen_job.id),
    )
    return GenerationPlan(generation_job=gen_job)
