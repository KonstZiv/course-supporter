"""Enqueue helpers for submitting jobs to ARQ with DB tracking.

The email helper (``enqueue_email``) is the fire-and-forget exception — no
``Job`` row, no DB tracking (Phase 6 R1's 4th enqueue form).
"""

from __future__ import annotations

import uuid

import structlog
from arq.connections import ArqRedis
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.config import get_settings
from course_supporter.job_priority import JobPriority
from course_supporter.jobs import JobType
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job, ProjectBase
from course_supporter.storage.project_base_repository import ProjectBaseRepository


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
    duration_sec: float | None = None,
) -> Job:
    """Create a Job record, enqueue ingestion to ARQ, flip entry to PENDING.

    Transaction-completing call (QQ5 ordering, mirrors
    :func:`enqueue_s3_cleanup`): the helper takes ownership of the commit.
    The caller STAGES its work in ``session`` (e.g. the document INSERT on
    create/confirm, the ``error_message`` reset on retry) but does NOT
    commit — this helper folds that staged work + the Job + the PENDING
    flip into one atomic commit, THEN enqueues the ARQ task post-commit,
    THEN a second commit records ``arq_job_id``. The caller must not commit
    again.

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
        duration_sec: Intake-probed source video duration (DD-3.3c-I-B);
            ``None`` for non-video ingests. Stored on the Job and summed by
            the admission gate; does not affect this call's own gate
            decision (the gate checks the EXISTING queue).

    Returns:
        The created Job with ``arq_job_id`` set.

    Raises:
        HTTPException: 503 ``INTAKE_OVERLOADED`` when the queue already holds
            at least ``intake_admission_max_pending_video_hours`` of pending
            video — the admission gate (DD-3.3c-I-B). Raised before any Job
            is created, so the caller's staged work rolls back uncommitted.
    """
    log = structlog.get_logger().bind(
        node_id=str(node_id), material_id=str(material_id)
    )
    job_repo = JobRepository(session)
    entry_repo = AuthoredDocumentRepository(session)

    # DD-3.3c-I-B admission gate (root-cause cap for intake > drain). The
    # single serial worker drains ~1.4x real-time, so an unbounded queue
    # grows forever under sustained itvdn intake; I-A's expires-bump only
    # deferred the symptom. Refuse intake when the EXISTING queue is at/over
    # the video-hours ceiling (the incoming item's own duration is NOT added
    # — we gate on what is already queued). First check, before any Job row.
    settings = get_settings()
    pending_hours = (await job_repo.sum_active_ingest_duration_sec()) / 3600
    if pending_hours >= settings.intake_admission_max_pending_video_hours:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INTAKE_OVERLOADED",
                "category": "queue_capacity",
                "details": (
                    f"Ingestion queue holds {pending_hours:.1f}h of pending "
                    f"video, at or above the "
                    f"{settings.intake_admission_max_pending_video_hours:.0f}h "
                    f"capacity ceiling. Retry in a few hours."
                ),
            },
            headers={
                "Retry-After": str(int(settings.intake_hint_check_after_hours * 3600))
            },
        )

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
        duration_sec=duration_sec,
    )

    # Stage the PENDING transition into the same transaction as the Job so
    # the UI reflects it atomically with the durable Job row.
    await entry_repo.set_pending(material_id, job.id)

    # QQ5 boundary (mirrors enqueue_s3_cleanup): commit the caller's staged
    # work + Job + PENDING BEFORE issuing the ARQ side-effect, so a hot
    # worker can never read a Job row that does not yet exist (the
    # "Job not found" race). The helper takes ownership of the commit.
    await session.commit()

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
        await session.commit()

    log.info(
        "job_enqueued",
        job_id=str(job.id),
        material_id=str(material_id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job


async def create_homework_job(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> Job:
    """Create the durable Job row for a homework submission and link it.

    DB-only — no ARQ dispatch. Creates the Job and stages the submission↔job
    link in the caller's session. The caller commits this together with its
    other staged work (student + submission) and THEN calls
    :func:`dispatch_homework`, so a hot worker can never read a Job/submission
    that is not yet committed (the DD-3.2.6-A "Job not found" race).

    Two helpers rather than the helper-owns-commit shape of
    :func:`enqueue_ingestion` because the homework file is the student's
    submission: dispatching after the caller's commit, OUTSIDE its S3-cleanup
    guard, means a dispatch failure leaves a durable, re-dispatchable
    submission with its file intact instead of deleting it.

    Args:
        session: Active DB session (caller controls + commits the transaction).
        tenant_id: Owning tenant UUID.
        submission_id: HomeworkSubmission to process.

    Returns:
        The created Job (not yet committed; no ``arq_job_id`` until dispatch).
    """
    job = await JobRepository(session).create(
        tenant_id=tenant_id,
        job_type="homework",
        input_params={
            "submission_id": str(submission_id),
        },
    )
    # Stage the submission↔job link into the same transaction as the Job, so
    # the caller's single commit makes both durable before dispatch.
    await HomeworkRepository(session).set_job_id(submission_id, job.id)
    return job


async def dispatch_homework(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    job_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> None:
    """Dispatch homework processing to ARQ AFTER the caller's commit (DD-3.2.6-A).

    Must be called only once the Job + submission are committed, so the worker
    always reads durable rows. Records ``arq_job_id`` in a second commit. A
    failure here leaves the committed submission intact for re-dispatch — it
    does not roll back the durable rows or delete the uploaded file.

    Args:
        redis: ARQ Redis connection pool.
        session: DB session (used only for the ``arq_job_id`` second commit).
        job_id: The committed Job's id.
        submission_id: HomeworkSubmission being processed.
    """
    log = structlog.get_logger().bind(submission_id=str(submission_id))
    arq_job = await redis.enqueue_job(
        "arq_process_homework",
        str(job_id),
        str(submission_id),
        _queue_name="homework",
    )
    if arq_job is not None:
        await JobRepository(session).set_arq_job_id(job_id, arq_job.job_id)
        await session.commit()
    log.info(
        "homework_job_dispatched",
        job_id=str(job_id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )


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
    Mirrors :func:`enqueue_ingestion` shape (QQ5, helper-owns-commit):
    durable-commit the Job row, THEN enqueue the ARQ task, THEN a second
    commit records ``arq_job_id``. The caller stages its work but must not
    commit again.

    Args:
        redis: ARQ Redis pool.
        session: Active DB session; the caller stages work but the helper
            takes ownership of the commit (does NOT leave it to the caller).
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

    # QQ5 boundary (mirrors enqueue_s3_cleanup): durable-commit the Job
    # BEFORE the ARQ dispatch so the hot worker never reads a Job row that
    # does not yet exist. The helper takes ownership of the commit.
    await session.commit()

    arq_job = await redis.enqueue_job(
        "arq_regenerate_node_summary",
        str(job.id),
        str(vertex_node_id),
        force,
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)
        await session.commit()

    log.info(
        "node_summary_job_enqueued",
        job_id=str(job.id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job


async def enqueue_base_normalize(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    archive_key: str,
) -> ProjectBase:
    """Create the next pending base version + its Job, enqueue normalization.

    KD18 P2. Helper-owns-commit (QQ5, mirrors :func:`enqueue_ingestion`): a
    Job-row-backed job WITHOUT any ``ExternalServiceCall`` (base normalization
    is deterministic, zero LLM). Ordering matters twice:

    1. ``create_version`` flushes FIRST — the concurrency collision point. Two
       racing re-uploads of the same document read the same
       ``MAX(version) + 1`` and the second flush raises ``IntegrityError`` on
       ``UNIQUE(authored_document_id, version)``. This helper does NOT wrap or
       swallow it (mirrors the repository): it propagates to the route for a
       clean 409, and because the flush fails BEFORE the Job insert, no Job row
       is created for the losing upload.
    2. The durable commit of the pending base + Job precedes the ARQ dispatch
       (DD-3.2.6-A): a hot worker can never read a Job whose ``ProjectBase`` is
       not yet committed. A second commit records ``arq_job_id``.

    Enqueue payload = ``(job_id, project_base_id)`` on the default queue; the
    worker re-reads ``archive_key`` from the ``ProjectBase`` by id.

    Returns:
        The created pending :class:`ProjectBase` — the route builds its
        response (version / id) from it.
    """
    log = structlog.get_logger().bind(authored_document_id=str(authored_document_id))
    job_repo = JobRepository(session)

    # (1) Allocate the pending base version first — the flush here is the
    # concurrency arbiter (UNIQUE); a collision raises before any Job exists.
    base = await ProjectBaseRepository(session).create_version(
        authored_document_id=authored_document_id,
        archive_key=archive_key,
    )
    # Durable Job row (BASE_NORMALIZE — deterministic, no ESC).
    job = await job_repo.create(
        tenant_id=tenant_id,
        job_type=JobType.BASE_NORMALIZE,
        input_params={"project_base_id": str(base.id)},
    )
    # (2) QQ5 boundary (mirrors enqueue_ingestion): durable-commit the pending
    # base + Job BEFORE the ARQ side-effect, so the worker never reads a Job
    # without its ProjectBase. The helper takes ownership of the commit.
    await session.commit()

    arq_job = await redis.enqueue_job(
        "base_normalize_task",
        str(job.id),
        str(base.id),
    )

    if arq_job is not None:
        await job_repo.set_arq_job_id(job.id, arq_job.job_id)
        await session.commit()

    log.info(
        "base_normalize_enqueued",
        project_base_id=str(base.id),
        version=base.version,
        job_id=str(job.id),
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return base


async def enqueue_email(
    *,
    arq: ArqRedis,
    message_id: str,
    to: str,
    context: dict[str, str],
) -> None:
    """Enqueue a transactional email on the default queue (Phase 6 R1/R2).

    The fire-and-forget 4th enqueue form: unlike the Job-backed helpers above,
    email creates NO ``Job`` row and stages no DB work — a direct
    ``arq.enqueue_job`` with ARQ's built-in retry (``max_tries``). Routes call
    this typed helper rather than ``arq.enqueue_job`` directly; ``message_id``
    and ``context`` are the ``email_text`` template id + its ``{placeholder}``
    values (e.g. ``password_reset`` + ``{reset_url, ttl}``). Log-only and
    PII-lean — the recipient domain, never the mailbox.
    """
    _, _, domain = to.rpartition("@")
    log = structlog.get_logger().bind(
        message_id=message_id,
        recipient_domain=domain or "?",
    )
    await arq.enqueue_job(
        "arq_send_email",
        message_id=message_id,
        to=to,
        context=context,
    )
    log.info("email_enqueued")
