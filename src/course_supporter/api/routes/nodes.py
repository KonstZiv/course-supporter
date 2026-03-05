"""Material tree node management API endpoints.

Provides CRUD operations for the hierarchical material tree.
Root nodes (parent_id IS NULL) serve as top-level entities (courses).
Tenant isolation is enforced by verifying node ownership via tenant_id.

Routes
------
- ``POST   /nodes``                           — Create root node (= course)
- ``GET    /nodes``                           — List root nodes (paginated)
- ``POST   /nodes/{nid}/children``            — Create child node
- ``GET    /nodes/{nid}/tree``                — Get full subtree
- ``GET    /nodes/{nid}/detail``              — Get subtree with materials
- ``GET    /nodes/{nid}``                     — Get single node
- ``PATCH  /nodes/{nid}``                     — Update node
- ``POST   /nodes/{nid}/move``                — Move node (reparent)
- ``POST   /nodes/{nid}/reorder``             — Reorder among siblings
- ``DELETE /nodes/{nid}``                     — Delete node (cascade)
- ``POST   /nodes/{nid}/slide-mapping``       — Create slide-video mappings
- ``GET    /nodes/{nid}/slide-mapping``        — List slide-video mappings
- ``DELETE /slide-mapping/{mid}``             — Delete a slide-video mapping
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    NodeCreateRequest,
    NodeListResponse,
    NodeMoveRequest,
    NodeReorderRequest,
    NodeResponse,
    NodeTreeResponse,
    NodeUpdateRequest,
    NodeWithMaterialsResponse,
    RejectedMappingResponse,
    SkippedMappingResponse,
    SlideVideoMapItemResponse,
    SlideVideoMapListResponse,
    SlideVideoMapRequest,
    SlideVideoMapResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.storage.mapping_validation import (
    MappingValidationError,
    MappingValidationResult,
    MappingValidationService,
    timecode_to_seconds,
)
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MappingValidationState, SlideVideoMapping
from course_supporter.storage.repositories import SlideVideoMappingRepository

logger = structlog.get_logger()

router = APIRouter(tags=["nodes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


def _ve_to_dict(err: MappingValidationError) -> dict[str, str | None]:
    """Convert MappingValidationError dataclass to a JSON-safe dict."""
    return asdict(err)


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the tenant.

    Raises:
        HTTPException 404: If the node is not found or
            does not belong to the authenticated tenant.
    """
    repo = MaterialNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


async def _require_child_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify a child node exists and belongs to the same tenant.

    Same as _require_node_for_tenant but with a clearer name for
    sub-node operations (slide-mapping, materials).
    """
    return await _require_node_for_tenant(session, tenant_id, node_id)


# ── Root node (= course) CRUD ──


@router.post("/nodes", status_code=201)
async def create_root_node(
    body: NodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a root node (course) in the material tree.

    Root nodes have no parent and appear at the top level.
    The ``order`` is auto-assigned as the next available position.
    """
    repo = MaterialNodeRepository(session)
    node = await repo.create(
        tenant_id=tenant.tenant_id,
        title=body.title,
        description=body.description,
    )
    await session.commit()

    logger.info(
        "root_node_created",
        node_id=str(node.id),
        tenant_id=str(tenant.tenant_id),
    )
    return NodeResponse.model_validate(node)


@router.get("/nodes")
async def list_root_nodes(
    tenant: SharedDep,
    session: SessionDep,
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of root nodes to return (1-100).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of root nodes to skip for pagination.",
    ),
) -> NodeListResponse:
    """List root nodes (courses) for the authenticated tenant.

    Returns a paginated list sorted by creation date (newest first).
    """
    repo = MaterialNodeRepository(session)
    roots = await repo.list_roots(tenant.tenant_id, limit=limit, offset=offset)
    total = await repo.count_roots(tenant.tenant_id)
    return NodeListResponse(
        items=[NodeResponse.model_validate(r) for r in roots],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Child node CRUD ──


@router.post("/nodes/{node_id}/children", status_code=201)
async def create_child_node(
    node_id: uuid.UUID,
    body: NodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a child node under an existing parent.

    The child inherits the tenant from the parent. The ``order``
    is auto-assigned as the next available position among siblings.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    node = await repo.create(
        tenant_id=tenant.tenant_id,
        parent_id=node_id,
        title=body.title,
        description=body.description,
    )
    await session.commit()

    logger.info(
        "child_node_created",
        node_id=str(node.id),
        parent_id=str(node_id),
    )
    return NodeResponse.model_validate(node)


# ── Tree operations ──


@router.get("/nodes/{node_id}/tree")
async def get_tree(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[NodeTreeResponse]:
    """Get the full subtree rooted at a node.

    Returns all nodes in a nested structure, with children
    recursively populated. Each level is sorted by ``order``.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = MaterialNodeRepository(session)
    roots = await repo.get_subtree(node_id)
    return [NodeTreeResponse.model_validate(r) for r in roots]


@router.get("/nodes/{node_id}/detail")
async def get_node_detail(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[NodeWithMaterialsResponse]:
    """Get the full subtree with materials attached to each node.

    Returns the hierarchical view including materials at each level
    and their lifecycle states. Includes a course-level fingerprint.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = MaterialNodeRepository(session)
    tree_roots = await repo.get_subtree(node_id, include_materials=True)
    return [NodeWithMaterialsResponse.model_validate(r) for r in tree_roots]


# ── Single node operations ──


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeResponse:
    """Get a single node by ID.

    Returns the flat node representation without children.
    Use ``GET /nodes/{id}/tree`` for the nested tree.
    """
    node = await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    return NodeResponse.model_validate(node)


@router.patch("/nodes/{node_id}")
async def update_node(
    node_id: uuid.UUID,
    body: NodeUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Update a node's title and/or description.

    Only fields present in the request body are updated.
    To clear the description, send ``"description": null``.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    # Distinguish "field omitted" from "field set to null"
    update_kwargs: dict[str, str | None] = {}
    if "title" in body.model_fields_set:
        update_kwargs["title"] = body.title
    if "description" in body.model_fields_set:
        update_kwargs["description"] = body.description

    node = await repo.update(node_id, **update_kwargs)
    await session.commit()

    logger.info("node_updated", node_id=str(node_id), fields=list(update_kwargs))
    return NodeResponse.model_validate(node)


@router.post("/nodes/{node_id}/move")
async def move_node(
    node_id: uuid.UUID,
    body: NodeMoveRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Move a node to a new parent (or to root).

    Cycle detection is enforced. Set ``parent_id`` to ``null``
    to make the node a root. Returns 422 if the move would create a cycle.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    # Validate target parent belongs to the same tenant
    if body.parent_id is not None:
        await _require_node_for_tenant(session, tenant.tenant_id, body.parent_id)

    try:
        node = await repo.move(node_id, body.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()

    logger.info(
        "node_moved",
        node_id=str(node_id),
        new_parent_id=str(body.parent_id),
    )
    return NodeResponse.model_validate(node)


@router.post("/nodes/{node_id}/reorder")
async def reorder_node(
    node_id: uuid.UUID,
    body: NodeReorderRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Reorder a node among its siblings.

    The target ``order`` is 0-based. If the value exceeds the
    number of siblings, it is clamped to the last position.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    try:
        node = await repo.reorder(node_id, body.order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()

    logger.info(
        "node_reordered",
        node_id=str(node_id),
        new_order=body.order,
    )
    return NodeResponse.model_validate(node)


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Delete a node and all its descendants.

    Deletion cascades to all child nodes and their attached
    material entries. This operation cannot be undone.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    await repo.delete(node_id)
    await session.commit()

    logger.info("node_deleted", node_id=str(node_id))


# ── Slide-Video Mapping ──


@router.post("/nodes/{node_id}/slide-mapping", status_code=201)
async def create_slide_mapping(
    node_id: uuid.UUID,
    body: SlideVideoMapRequest,
    tenant: PrepDep,
    session: SessionDep,
    response: Response,
) -> SlideVideoMapResponse:
    """Create slide-video mappings for a material node.

    Supports partial success: valid mappings are created even when some
    fail validation. Duplicate mappings (same natural key) are skipped.

    Returns:
        201 — all created (or only skipped duplicates, none rejected).
        207 — partial success (some created, some rejected).
        422 — all failed (none created, all rejected).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # ── Validation (L1 structural + L2 content + L3 deferred) ──
    validator = MappingValidationService(session)
    results = await validator.validate_batch(node_id, body.mappings)

    # ── Deduplication — natural key check ──
    svm_repo = SlideVideoMappingRepository(session)
    existing = await svm_repo.get_by_node_id(node_id)
    existing_keys: set[tuple[str, str, int, int]] = {
        (
            str(m.presentation_entry_id),
            str(m.video_entry_id),
            m.slide_number,
            timecode_to_seconds(m.video_timecode_start),
        )
        for m in existing
    }

    # ── Classify each mapping ──
    rejected: list[RejectedMappingResponse] = []
    skipped_items: list[SkippedMappingResponse] = []
    creatable_mappings: list[SlideVideoMapEntry] = []
    creatable_results: list[MappingValidationResult] = []

    for idx, (mapping, vr) in enumerate(zip(body.mappings, results, strict=True)):
        if vr.status == MappingValidationState.VALIDATION_FAILED:
            rejected.append(
                RejectedMappingResponse(
                    index=idx,
                    errors=[_ve_to_dict(e) for e in vr.errors],
                )
            )
            continue

        natural_key = (
            str(mapping.presentation_entry_id),
            str(mapping.video_entry_id),
            mapping.slide_number,
            timecode_to_seconds(mapping.video_timecode_start),
        )
        if natural_key in existing_keys:
            skipped_items.append(
                SkippedMappingResponse(index=idx, hint="already exists")
            )
            continue

        # Re-index validation result for the creatable list
        creatable_results.append(
            MappingValidationResult(
                index=len(creatable_mappings),
                status=vr.status,
                errors=vr.errors,
                blocking_factors=vr.blocking_factors,
            )
        )
        creatable_mappings.append(mapping)

    # ── Create + respond ──
    records: list[SlideVideoMapping] = []
    if creatable_mappings:
        records = await svm_repo.batch_create(
            node_id, creatable_mappings, validation_results=creatable_results
        )
        await session.commit()

    # ── HTTP status code ──
    if not records and rejected:
        response.status_code = 422
    elif records and rejected:
        response.status_code = 207
    else:
        response.status_code = 201

    # ── Hints ──
    hints: dict[str, str] = {}
    if rejected:
        hints["resubmit"] = (
            "Fix errors in rejected items and resubmit only those. "
            "Already created mappings will be automatically skipped."
        )
        hints["batch_size"] = "If the batch keeps failing, try reducing batch size."

    return SlideVideoMapResponse(
        created=len(records),
        skipped=len(skipped_items),
        failed=len(rejected),
        mappings=[SlideVideoMapItemResponse.model_validate(r) for r in records],
        skipped_items=skipped_items,
        rejected=rejected,
        hints=hints,
    )


@router.get("/nodes/{node_id}/slide-mapping")
async def list_slide_mappings(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> SlideVideoMapListResponse:
    """List all slide-video mappings for a material tree node.

    Returns mappings sorted by ``order`` (ascending, 0-based).
    An empty list is returned when the node has no mappings (not a 404).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    svm_repo = SlideVideoMappingRepository(session)
    mappings = await svm_repo.get_by_node_id(node_id)
    return SlideVideoMapListResponse(
        items=[SlideVideoMapItemResponse.model_validate(m) for m in mappings],
        total=len(mappings),
    )


@router.delete("/slide-mapping/{mapping_id}", status_code=204)
async def delete_slide_mapping(
    mapping_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Delete a single slide-video mapping.

    Ownership verification: mapping → node → tenant chain.
    """
    svm_repo = SlideVideoMappingRepository(session)
    mapping = await svm_repo.get_by_id(mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found")

    # Ownership: mapping → node → tenant
    node_repo = MaterialNodeRepository(session)
    node = await node_repo.get_by_id(mapping.node_id)
    if node is None or node.tenant_id != tenant.tenant_id:
        raise HTTPException(
            status_code=404,
            detail="Mapping not found",
        )

    await svm_repo.delete(mapping)
    await session.commit()
