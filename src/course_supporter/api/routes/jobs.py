"""Job status + reactivate API endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, NamedTuple

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import (
    JobListItemResponse,
    JobListResponse,
    JobResponse,
    MaterialHistoryItemResponse,
    MaterialHistoryResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import (
    AUTHOR_JOB_TYPES,
    AuthorJobRow,
    JobRepository,
    StateClass,
)
from course_supporter.storage.job_state import derive_job_state
from course_supporter.storage.orm import Job

router = APIRouter(tags=["jobs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]


class _ReenqueueDispatch(NamedTuple):
    """ARQ task call shape resolved from a failed Job's state.

    ``args`` are positional task args passed to the ARQ function;
    ``task_kwargs`` are keyword args (used by tasks with kw-only
    parameters like :func:`s3_cleanup_task`); ``queue_name`` selects
    a non-default ARQ queue (e.g. ``"homework"``).
    """

    arq_function: str
    args: list[Any]
    queue_name: str | None = None
    task_kwargs: dict[str, Any] | None = None


def _resolve_reenqueue(job: Job) -> _ReenqueueDispatch:
    """Map ``(job.job_type, job.subject_id, job.input_params)`` → ARQ call.

    Dispatch over the canonical :class:`JobType` values (matched by
    enum member, not string literal — contract invariant #3).

    L1b: the identity argument of each branch comes from the typed
    ``job.subject_id`` column, not ``input_params``; the remaining
    payload keys (``source_type`` / ``source_url`` / ``force`` /
    ``file_keys``) stay in ``input_params`` — they are task parameters,
    not identity.

    Raises :class:`HTTPException` (422) when:

    * ``job.job_type`` is not in the supported set.
    * an entity-typed job has a NULL ``subject_id`` (unrecoverable
      historical row) — the same human 422 the missing input_params
      key used to give.
    * ``job.input_params`` is missing a required payload field.
    """
    p = job.input_params or {}
    jid = str(job.id)

    def _subject() -> str:
        # Identity now lives in the typed subject_id column (L1b). A NULL
        # subject_id (historical row predating typed subjects) yields the same
        # human 422 the missing input_params identity key used to give.
        if job.subject_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Job {job.id} has no subject_id (legacy row predating the "
                    f"typed subject); cannot re-enqueue."
                ),
            )
        return str(job.subject_id)

    try:
        match job.job_type:
            case JobType.DOCUMENT_PROCESSING:
                return _ReenqueueDispatch(
                    arq_function="arq_ingest_material",
                    args=[
                        jid,
                        _subject(),
                        p["source_type"],
                        p["source_url"],
                        job.priority,
                    ],
                )
            case JobType.HOMEWORK_PROCESSING:
                return _ReenqueueDispatch(
                    arq_function="arq_process_homework",
                    args=[jid, _subject()],
                    queue_name="homework",
                )
            case JobType.S3_CLEANUP:
                # KD13 s3_cleanup_task uses kw-only args; carried via
                # task_kwargs rather than positional ``args``. No subject —
                # ``file_keys`` is a payload list, not identity.
                return _ReenqueueDispatch(
                    arq_function="s3_cleanup_task",
                    args=[],
                    task_kwargs={
                        "file_keys": p["file_keys"],
                        "job_id": jid,
                    },
                )
            case JobType.NODE_SUMMARY_REGENERATION:
                # Phase 3.2.4 — the methodist two-pass orchestrator
                # job. The vertex node id is the subject; ``force`` is a
                # payload flag replayed verbatim so reactivate resumes the
                # same scope; ``stage_progress`` is preserved by
                # ``reactivate`` so the orchestrator picks up from
                # KD4a checkpoint via memo-skip on already-fresh nodes.
                return _ReenqueueDispatch(
                    arq_function="arq_regenerate_node_summary",
                    args=[
                        jid,
                        _subject(),
                        p.get("force", False),
                    ],
                )
            case _:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Reactivate not supported for job_type="
                        f"{job.job_type!r}. Supported types: "
                        f"document_processing, homework_processing, "
                        f"s3_cleanup, node_summary_regeneration."
                    ),
                )
    except KeyError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Job {job.id} input_params missing required field for "
                f"re-enqueue: {exc.args[0]!r}."
            ),
        ) from exc


def _to_list_item(row: AuthorJobRow) -> JobListItemResponse:
    """Map a repository row → door-1 item, computing the derived job-state token
    and the deleted-source flag (the repository stays free of API shapes)."""
    job = row.job
    return JobListItemResponse(
        id=job.id,
        job_type=job.job_type,
        job_state=derive_job_state(job.status),
        queued_at=job.queued_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        subject_type=job.subject_type,
        subject_id=job.subject_id,
        material_id=row.material_id,
        display_name=row.display_name,
        display_deleted=row.material_deleted_at is not None,
        display_deleted_at=row.material_deleted_at,
        material_source_type=row.material_source_type,
        base_version=row.base_version,
        current_stage=job.current_stage,
        stage_progress=job.stage_progress,
    )


# Static paths declared BEFORE ``/jobs/{job_id}`` so the parameterised route
# never shadows them (the uuid convertor already rejects "history", but explicit
# ordering keeps intent clear).
@router.get("/jobs")
async def list_jobs(
    tenant: PrepDep,
    session: SessionDep,
    state_class: Annotated[StateClass | None, Query()] = None,
    completed_after: Annotated[datetime | None, Query()] = None,
    material_id: Annotated[uuid.UUID | None, Query()] = None,
    job_types: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """List the author's work — live plus recent (step A door 1).

    Author scope only (``require_scope(PREP)``) — deliberately tighter than the
    single-job ``GET /jobs/{id}``, which stays shared with the check contour;
    these doors never return homework/cleanup work. Filters mirror the storage
    method (§4); an out-of-universe ``job_types`` value is a 422.
    """
    if job_types is not None:
        invalid = sorted(set(job_types) - AUTHOR_JOB_TYPES)
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"job_types outside the author universe: {invalid}. "
                    f"Allowed: {sorted(AUTHOR_JOB_TYPES)}."
                ),
            )
    repo = JobRepository(session)
    rows = await repo.list_author_jobs(
        tenant.tenant_id,
        state_class=state_class,
        completed_after=completed_after,
        material_id=material_id,
        job_types=job_types,
        limit=limit,
        offset=offset,
    )
    total = await repo.count_author_jobs(
        tenant.tenant_id,
        state_class=state_class,
        completed_after=completed_after,
        material_id=material_id,
        job_types=job_types,
    )
    return JobListResponse(
        items=[_to_list_item(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/history")
async def list_job_history(
    tenant: PrepDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MaterialHistoryResponse:
    """History of the author's materials — one row per material, latest work as
    current state plus a work count (step A door 2). Grouped server-side."""
    repo = JobRepository(session)
    rows = await repo.list_author_material_history(
        tenant.tenant_id, limit=limit, offset=offset
    )
    total = await repo.count_author_material_anchors(tenant.tenant_id)
    return MaterialHistoryResponse(
        items=[
            MaterialHistoryItemResponse(
                material_id=r.material_id,
                display_name=r.display_name,
                material_deleted=r.material_deleted_at is not None,
                material_deleted_at=r.material_deleted_at,
                material_source_type=r.material_source_type,
                processing_phase=r.processing_phase,
                last_job=_to_list_item(r.last_job),
                jobs_count=r.jobs_count,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> JobResponse:
    """Get job status by ID.

    Tenant isolation enforced via job.course_node_id → node.tenant_id,
    with fallback to job.tenant_id. Returns 404 if the job does
    not exist or does not belong to the current tenant.
    """
    repo = JobRepository(session)
    job = await repo.get_by_id_for_tenant(job_id, tenant.tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.model_validate(job)


@router.post("/jobs/{job_id}/reactivate")
async def reactivate_job(
    job_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> JobResponse:
    """Reactivate a failed Job for retry (vision §3 KD13).

    DB transition: ``status: 'failed' → 'queued'``, clears
    ``error_message`` and run-attempt timestamps, preserves
    ``stage_progress`` (worker resumes from KD4a checkpoint) and
    ``queued_at`` (first-queued metadata). Then enqueues a new ARQ
    task via the legacy-aware dispatcher and stores the new
    ``arq_job_id`` on the Job. All in one outer transaction; failure
    rolls back, leaving the Job in its original ``failed`` state.

    **Per-entity state caveat.** Reactivate transitions only the
    ``Job`` row — entity-level state (e.g. ``AuthoredDocument`` ingestion
    state, ``HomeworkSubmission`` review state) is NOT touched. Workers
    must defensively reset entity-level state on task entry (e.g.
    ``AuthoredDocumentRepository.set_pending`` is called by
    ``arq_ingest_material`` itself for exactly this reason). If a
    worker lacks this guard, entity state may diverge from Job state;
    use the per-entity retry endpoint (e.g. ``materials.py /retry``)
    when that semantics matters more than the ``stage_progress``
    resume that ``reactivate`` provides.

    Returns 200 with the updated Job representation. Errors:

    * **404** — Job not found or not visible to current tenant.
    * **409** — Job is in a state other than ``'failed'`` (or is
      soft-deleted).
    * **422** — ``job_type`` not supported by the dispatcher, or
      ``input_params`` missing required fields for re-enqueue.
    """
    repo = JobRepository(session)
    job = await repo.get_by_id_for_tenant(job_id, tenant.tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # L1b: reactivating failed→queued re-enters the in-flight set of
    # uq_jobs_subject_in_flight. If a newer job already occupies that slot for
    # the same subject, block with a human 409 instead of a 500 from the index
    # (invariant 5). NULL-subject jobs cannot conflict — the check skips them.
    existing = await repo.get_inflight_job_for_subject(job.subject_type, job.subject_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUBJECT_ALREADY_PROCESSING",
                "details": (
                    f"another in-flight job {existing.id} already covers this "
                    f"subject; wait for it to finish before reactivating."
                ),
            },
        )

    try:
        await repo.reactivate(job.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        # A concurrent reactivate/enqueue won the subject slot between the
        # app-check above and this flush — the index is the final judge
        # (invariant 4). Present it as a 409, never a 500 (invariant 5).
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUBJECT_ALREADY_PROCESSING",
                "details": (
                    "a concurrent job won the subject slot; retry once it finishes."
                ),
            },
        ) from exc

    dispatch = _resolve_reenqueue(job)

    enqueue_kwargs: dict[str, Any] = {}
    if dispatch.queue_name is not None:
        enqueue_kwargs["_queue_name"] = dispatch.queue_name
    task_kwargs = dispatch.task_kwargs or {}

    arq_job = await arq.enqueue_job(
        dispatch.arq_function,
        *dispatch.args,
        **task_kwargs,
        **enqueue_kwargs,
    )
    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    await session.commit()

    return JobResponse.model_validate(job)
