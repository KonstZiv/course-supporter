"""Structure generation API endpoints.

Provides endpoints for triggering course structure generation,
checking latest results, and browsing generation history.

Routes
------
- ``POST  /nodes/{nid}/generate``                     — Trigger generation
- ``GET   /nodes/{nid}/structure``                     — Latest snapshot
- ``GET   /nodes/{nid}/structure/history``             — Snapshot list (metadata)
- ``GET   /nodes/{nid}/structure/snapshots/{snap_id}`` — Snapshot detail
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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
    MappingWarningResponse,
    SnapshotDetailResponse,
    SnapshotListResponse,
    SnapshotSummaryResponse,
    StructureNodeResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.errors import (
    GenerationConflictError,
    NodeNotFoundError,
    NoReadyMaterialsError,
)
from course_supporter.generation_orchestrator import trigger_generation
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import StructureNode
from course_supporter.storage.snapshot_repository import SnapshotRepository

logger = structlog.get_logger()

router = APIRouter(tags=["generation"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the tenant."""
    repo = MaterialNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


async def _find_root_id(
    session: AsyncSession,
    node_id: uuid.UUID,
) -> uuid.UUID:
    """Walk up parent chain to find the root node ID."""
    repo = MaterialNodeRepository(session)
    current_id = node_id
    while True:
        node = await repo.get_by_id(current_id)
        if node is None:
            raise HTTPException(
                status_code=500,
                detail="Data inconsistency: parent node not found during root lookup",
            )
        if node.parent_id is None:
            return node.id
        current_id = node.parent_id


@router.post("/nodes/{node_id}/generate", status_code=202)
async def generate_structure(
    node_id: uuid.UUID,
    body: GenerateRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
    response: Response,
) -> GenerationPlanResponse:
    """Trigger structure generation for a node's subtree.

    Returns 200 if an identical snapshot already exists (idempotent),
    202 if new generation work was enqueued, 404 if node not found,
    409 if an active generation overlaps, or 422 if the target subtree
    has no materials.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # Find root for full-tree context (conflict detection)
    root_node_id = await _find_root_id(session, node_id)
    # If the target IS the root, target_node_id is None (whole tree)
    target_node_id = None if node_id == root_node_id else node_id

    try:
        plan = await trigger_generation(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            root_node_id=root_node_id,
            target_node_id=target_node_id,
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
        node_id=str(node_id),
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
        mapping_warnings=[
            MappingWarningResponse.model_validate(w) for w in plan.mapping_warnings
        ],
    )


async def _build_snapshot_detail(
    session: AsyncSession,
    snapshot: object,
) -> SnapshotDetailResponse:
    """Build SnapshotDetailResponse with structure_tree from DB."""
    from course_supporter.storage.structure_node_repository import (
        StructureNodeRepository,
    )

    resp = SnapshotDetailResponse.model_validate(snapshot)
    sn_repo = StructureNodeRepository(session)
    flat_nodes = await sn_repo.get_tree(resp.id)

    if flat_nodes:
        resp.structure_tree = _flat_to_tree(flat_nodes)
    return resp


def _flat_to_tree(
    flat_nodes: Sequence[StructureNode],
) -> list[StructureNodeResponse]:
    """Convert flat node list into nested StructureNodeResponse tree."""
    node_map: dict[uuid.UUID, StructureNodeResponse] = {}
    roots: list[StructureNodeResponse] = []

    for n in flat_nodes:
        resp = StructureNodeResponse.model_validate(n)
        resp.children = []
        node_map[resp.id] = resp

    for n in flat_nodes:
        resp = node_map[n.id]
        if n.parent_structurenode_id is None:
            roots.append(resp)
        elif n.parent_structurenode_id in node_map:
            node_map[n.parent_structurenode_id].children.append(resp)

    return roots


@router.get("/nodes/{node_id}/structure")
async def get_latest_structure(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> SnapshotDetailResponse:
    """Get the latest generated structure for a node.

    Returns 404 if no snapshot exists yet.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = SnapshotRepository(session)
    snapshot = await repo.get_latest_for_node(node_id)

    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="No structure snapshot found.",
        )

    return await _build_snapshot_detail(session, snapshot)


@router.get("/nodes/{node_id}/structure/history")
async def list_snapshots(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100, description="Max items per page."),
    offset: int = Query(default=0, ge=0, description="Items to skip."),
) -> SnapshotListResponse:
    """List structure snapshots for a node (metadata only).

    Returns all snapshots newest-first with pagination.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = SnapshotRepository(session)
    total = await repo.count_for_node(node_id)
    page = await repo.list_for_node(node_id, limit=limit, offset=offset)

    return SnapshotListResponse(
        items=[SnapshotSummaryResponse.model_validate(s) for s in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/nodes/{node_id}/structure/snapshots/{snapshot_id}")
async def get_snapshot(
    node_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> SnapshotDetailResponse:
    """Get a specific structure snapshot by ID.

    Returns 404 if the snapshot does not exist or belongs
    to a different node.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = SnapshotRepository(session)
    snapshot = await repo.get_by_id(snapshot_id)

    if snapshot is None or snapshot.node_id != node_id:
        raise HTTPException(
            status_code=404,
            detail="Snapshot not found.",
        )

    return await _build_snapshot_detail(session, snapshot)
