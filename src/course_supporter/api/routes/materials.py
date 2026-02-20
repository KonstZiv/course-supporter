"""Material entry management API endpoints.

Provides CRUD operations for materials attached to tree nodes.
Each material goes through a lifecycle (RAW → PENDING → READY/ERROR)
tracked via the derived ``state`` property. Ingestion is auto-enqueued
on creation and can be retried on failure.

Tenant isolation is enforced by verifying course ownership
before accessing any node or material.

Routes
------
- ``POST   /courses/{id}/nodes/{nid}/materials``       — Add material to node
- ``GET    /courses/{id}/nodes/{nid}/materials``        — List materials for node
- ``GET    /courses/{id}/materials/{mid}``              — Get single material
- ``DELETE /courses/{id}/materials/{mid}``              — Delete material
- ``POST   /courses/{id}/materials/{mid}/retry``        — Retry failed ingestion
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import (
    MaterialEntryCreateRequest,
    MaterialEntryCreateResponse,
    MaterialEntryResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_ingestion
from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MaterialEntry
from course_supporter.storage.repositories import CourseRepository

logger = structlog.get_logger()

router = APIRouter(tags=["materials"])

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


async def _require_node(
    session: AsyncSession,
    node_id: uuid.UUID,
    course_id: uuid.UUID,
) -> None:
    """Verify the node exists and belongs to the course.

    Raises:
        HTTPException 404: If the node is not found or
            belongs to a different course.
    """
    repo = MaterialNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.course_id != course_id:
        raise HTTPException(status_code=404, detail="Node not found")


async def _require_material(
    entry_repo: MaterialEntryRepository,
    node_repo: MaterialNodeRepository,
    entry_id: uuid.UUID,
    course_id: uuid.UUID,
) -> MaterialEntry:
    """Verify the material exists and belongs to the course.

    Checks MaterialEntry → MaterialNode → Course chain.

    Raises:
        HTTPException 404: If the material is not found or
            belongs to a different course.
    """
    entry = await entry_repo.get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Material not found")

    node = await node_repo.get_by_id(entry.node_id)
    if node is None or node.course_id != course_id:
        raise HTTPException(status_code=404, detail="Material not found")
    return entry


@router.post("/courses/{course_id}/nodes/{node_id}/materials", status_code=201)
async def create_material(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: MaterialEntryCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> MaterialEntryCreateResponse:
    """Add a new material to a tree node.

    Creates a ``MaterialEntry`` and auto-enqueues an ingestion job
    via ARQ. The ``job_id`` in the response can be used to track
    processing status via ``GET /api/v1/jobs/{job_id}``.

    The material starts in ``raw`` state and transitions to
    ``pending`` once the ingestion job picks it up.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    await _require_node(session, node_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    entry = await entry_repo.create(
        node_id=node_id,
        source_type=body.source_type,
        source_url=body.source_url,
        filename=body.filename,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        course_id=course_id,
        material_id=entry.id,
        source_type=body.source_type,
        source_url=body.source_url,
    )
    await session.commit()

    logger.info(
        "material_entry_created",
        entry_id=str(entry.id),
        node_id=str(node_id),
        course_id=str(course_id),
        job_id=str(job.id),
    )
    return MaterialEntryCreateResponse(
        id=entry.id,
        node_id=entry.node_id,
        source_type=entry.source_type,
        source_url=entry.source_url,
        filename=entry.filename,
        order=entry.order,
        state=entry.state,
        job_id=job.id,
        created_at=entry.created_at,
    )


@router.get("/courses/{course_id}/nodes/{node_id}/materials")
async def list_materials(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[MaterialEntryResponse]:
    """List all materials attached to a tree node.

    Returns materials ordered by their position (``order`` field).
    Each material includes the derived ``state`` indicating its
    lifecycle stage.

    Returns an empty list if the node has no materials.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    await _require_node(session, node_id, course_id)

    repo = MaterialEntryRepository(session)
    entries = await repo.get_for_node(node_id)
    return [MaterialEntryResponse.model_validate(e) for e in entries]


@router.get("/courses/{course_id}/materials/{entry_id}")
async def get_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> MaterialEntryResponse:
    """Get a single material entry by ID.

    The material must belong to the specified course
    (verified through the node → course chain).
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)
    return MaterialEntryResponse.model_validate(entry)


@router.delete("/courses/{course_id}/materials/{entry_id}", status_code=204)
async def delete_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Delete a material entry.

    Removes the material and its processed content permanently.
    If an ingestion job is in progress, it will fail gracefully
    when it tries to write back results.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)

    await entry_repo.delete(entry.id)
    await session.commit()

    logger.info(
        "material_entry_deleted",
        entry_id=str(entry_id),
        course_id=str(course_id),
    )


@router.post("/courses/{course_id}/materials/{entry_id}/retry")
async def retry_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> MaterialEntryCreateResponse:
    """Retry ingestion for a failed material.

    Only materials in ``error`` state can be retried. This clears
    the error, creates a new ingestion job, and returns the updated
    material with the new ``job_id``.

    Returns 409 if the material is not in ``error`` state.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)

    if entry.state != "error":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry: material is in '{entry.state}' state, expected 'error'."
            ),
        )

    # Clear error and re-enqueue
    entry.error_message = None
    await session.flush()

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        course_id=course_id,
        material_id=entry.id,
        source_type=entry.source_type,
        source_url=entry.source_url,
    )
    await session.commit()

    logger.info(
        "material_entry_retry",
        entry_id=str(entry_id),
        course_id=str(course_id),
        job_id=str(job.id),
    )
    return MaterialEntryCreateResponse(
        id=entry.id,
        node_id=entry.node_id,
        source_type=entry.source_type,
        source_url=entry.source_url,
        filename=entry.filename,
        order=entry.order,
        state=entry.state,
        job_id=job.id,
        created_at=entry.created_at,
    )
