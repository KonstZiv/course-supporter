"""Job status + reactivate API endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, NamedTuple

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import JobResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job

router = APIRouter(tags=["jobs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]


class _ReenqueueDispatch(NamedTuple):
    """ARQ task call shape resolved from a failed Job's state."""

    arq_function: str
    args: list[Any]
    queue_name: str | None = None


def _resolve_reenqueue(job: Job) -> _ReenqueueDispatch:
    """Map ``(job.job_type, job.input_params)`` → ARQ task call shape.

    Transitional dispatch over the legacy ``job_type`` strings
    currently emitted by ``enqueue.py`` and ``methodist_orchestrator``.
    Phase 2.x rewrite of orchestrators to KD13's 4-value :class:`JobType`
    enum will collapse this to a 4-case match (and will likely add a
    ``s3_cleanup`` arm — currently the ``s3_cleanup_task`` worker
    lands in commit (g) of task 0.3 and a future commit will register
    the ``s3_cleanup`` enum value here).

    Raises :class:`HTTPException` (422) when:

    * ``job.job_type`` is not in the supported set.
    * ``job.input_params`` is missing a required field for the
      target ARQ task.
    """
    p = job.input_params or {}
    jid = str(job.id)

    try:
        match job.job_type:
            case "ingest":
                return _ReenqueueDispatch(
                    arq_function="arq_ingest_material",
                    args=[
                        jid,
                        p["material_id"],
                        p["source_type"],
                        p["source_url"],
                        job.priority,
                    ],
                )
            case "generate_structure":
                return _ReenqueueDispatch(
                    arq_function="arq_generate_structure",
                    args=[
                        jid,
                        p["root_node_id"],
                        p.get("target_node_id"),
                        p["mode"],
                    ],
                )
            case "generate" | "reconcile" | "refine":
                return _ReenqueueDispatch(
                    arq_function="arq_execute_step",
                    args=[
                        jid,
                        p["root_node_id"],
                        p["target_node_id"],
                        p["mode"],
                        p["step_type"],
                    ],
                )
            case "reconcile_preview":
                return _ReenqueueDispatch(
                    arq_function="arq_reconcile_preview",
                    args=[jid, p["node_id"]],
                )
            case "homework":
                return _ReenqueueDispatch(
                    arq_function="arq_process_homework",
                    args=[jid, p["submission_id"]],
                    queue_name="homework",
                )
            case "methodist_bottom_up" | "methodist_top_down":
                return _ReenqueueDispatch(
                    arq_function="arq_execute_methodist_step",
                    args=[
                        jid,
                        p["materialnode_id"],
                        p["editable_id"],
                        p["phase"],
                    ],
                )
            case _:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Reactivate not supported for job_type="
                        f"{job.job_type!r}. Supported types: ingest, "
                        f"generate_structure, reconcile_preview, generate, "
                        f"reconcile, refine, homework, methodist_bottom_up, "
                        f"methodist_top_down."
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
    ``Job`` row — entity-level state (e.g. ``MaterialEntry`` ingestion
    state, ``HomeworkSubmission`` review state) is NOT touched. Workers
    must defensively reset entity-level state on task entry (e.g.
    ``MaterialEntryRepository.set_pending`` is called by
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

    try:
        await repo.reactivate(job.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch = _resolve_reenqueue(job)

    enqueue_kwargs: dict[str, Any] = {}
    if dispatch.queue_name is not None:
        enqueue_kwargs["_queue_name"] = dispatch.queue_name

    arq_job = await arq.enqueue_job(
        dispatch.arq_function, *dispatch.args, **enqueue_kwargs
    )
    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    await session.commit()

    return JobResponse.model_validate(job)
