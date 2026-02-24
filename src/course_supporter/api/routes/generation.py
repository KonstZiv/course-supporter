"""Structure generation API endpoints.

Provides endpoints for triggering course structure generation,
checking latest results, and browsing generation history.

Routes
------
- ``POST  /courses/{id}/generate``                     — Trigger generation
- ``GET   /courses/{id}/structure``                     — Latest snapshot
- ``GET   /courses/{id}/structure/history``             — Snapshot list (metadata)
- ``GET   /courses/{id}/structure/snapshots/{snap_id}`` — Snapshot detail
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import (
    GenerateRequest,
    GenerationPlanResponse,
    JobResponse,
    SnapshotDetailResponse,
    SnapshotListResponse,
    SnapshotSummaryResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.errors import (
    GenerationConflictError,
    NodeNotFoundError,
    NoReadyMaterialsError,
)
from course_supporter.generation_orchestrator import trigger_generation
from course_supporter.storage.repositories import CourseRepository
from course_supporter.storage.snapshot_repository import SnapshotRepository

logger = structlog.get_logger()

router = APIRouter(tags=["generation"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]
SharedDep = Annotated[TenantContext, Depends(require_scope("prep", "check"))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


async def _require_course(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    course_id: uuid.UUID,
) -> None:
    """Verify the course exists and belongs to the tenant.

    Raises:
        HTTPException 404: If the course is not found or
            does not belong to the authenticated tenant.
    """
    repo = CourseRepository(session, tenant_id)
    course = await repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")


@router.post("/courses/{course_id}/generate", status_code=202)
async def generate_structure(
    course_id: uuid.UUID,
    body: GenerateRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
    response: Response,
) -> GenerationPlanResponse:
    """Trigger structure generation for a course or subtree.

    Returns 200 if an identical snapshot already exists (idempotent),
    202 if new generation work was enqueued, 404 if course/node not
    found, 409 if an active generation overlaps, or 422 if the target
    subtree has no materials.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    try:
        plan = await trigger_generation(
            redis=arq,
            session=session,
            course_id=course_id,
            node_id=body.node_id,
            mode=body.mode.value,
        )
    except NodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Node not found") from exc
    except GenerationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Generation conflict: {exc.conflict.reason}",
        ) from exc
    except NoReadyMaterialsError as exc:
        raise HTTPException(
            status_code=422,
            detail="No materials found in the target subtree.",
        ) from exc

    await session.commit()

    if plan.is_idempotent:
        response.status_code = 200

    logger.info(
        "generation_triggered",
        course_id=str(course_id),
        node_id=str(body.node_id),
        mode=body.mode,
        is_idempotent=plan.is_idempotent,
        generation_job_id=(
            str(plan.generation_job.id) if plan.generation_job else None
        ),
        ingestion_count=len(plan.ingestion_jobs),
    )

    return GenerationPlanResponse(
        generation_job=(
            JobResponse.model_validate(plan.generation_job)
            if plan.generation_job
            else None
        ),
        ingestion_jobs=[JobResponse.model_validate(j) for j in plan.ingestion_jobs],
        existing_snapshot_id=plan.existing_snapshot_id,
        is_idempotent=plan.is_idempotent,
    )


@router.get("/courses/{course_id}/structure")
async def get_latest_structure(
    course_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
    node_id: Annotated[
        uuid.UUID | None,
        Query(description="Node UUID for node-level snapshot. Omit for course-level."),
    ] = None,
) -> SnapshotDetailResponse:
    """Get the latest generated structure for a course or node.

    Returns 404 if no snapshot exists yet.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    repo = SnapshotRepository(session)
    if node_id is not None:
        snapshot = await repo.get_latest_for_node(course_id, node_id)
    else:
        snapshot = await repo.get_latest_for_course(course_id)

    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="No structure snapshot found.",
        )

    return SnapshotDetailResponse.model_validate(snapshot)


@router.get("/courses/{course_id}/structure/history")
async def list_snapshots(
    course_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100, description="Max items per page."),
    offset: int = Query(default=0, ge=0, description="Items to skip."),
) -> SnapshotListResponse:
    """List structure snapshots for a course (metadata only).

    Returns all snapshots newest-first with pagination.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    repo = SnapshotRepository(session)
    total = await repo.count_for_course(course_id)
    page = await repo.list_for_course(course_id, limit=limit, offset=offset)

    return SnapshotListResponse(
        items=[SnapshotSummaryResponse.model_validate(s) for s in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/courses/{course_id}/structure/snapshots/{snapshot_id}")
async def get_snapshot(
    course_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> SnapshotDetailResponse:
    """Get a specific structure snapshot by ID.

    Returns 404 if the snapshot does not exist or belongs
    to a different course.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    repo = SnapshotRepository(session)
    snapshot = await repo.get_by_id(snapshot_id)

    if snapshot is None or snapshot.course_id != course_id:
        raise HTTPException(
            status_code=404,
            detail="Snapshot not found.",
        )

    return SnapshotDetailResponse.model_validate(snapshot)
