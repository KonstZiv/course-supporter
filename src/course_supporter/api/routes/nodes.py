"""Material tree node management API endpoints.

Provides CRUD operations for the hierarchical material tree.
Root nodes (parent_materialnode_id IS NULL) serve as top-level entities (courses).
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
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_s3_client, get_session
from course_supporter.api.schemas import (
    NodeCreateRequest,
    NodeListResponse,
    NodeMoveRequest,
    NodeReorderRequest,
    NodeResponse,
    NodeTreeResponse,
    NodeUpdateRequest,
    NodeWithMaterialsResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MaterialNode
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

router = APIRouter(tags=["nodes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


def _node_response(node: MaterialNode) -> NodeResponse:
    """Build NodeResponse with computed children_count and materials_count."""
    resp = NodeResponse.model_validate(node)
    resp.children_count = len(node.children) if node.children else 0
    resp.materials_count = len(node.materials) if node.materials else 0
    return resp


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
        default_language=body.default_language,
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
        items=[_node_response(r) for r in roots],
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
        parent_materialnode_id=node_id,
        title=body.title,
        description=body.description,
        default_language=body.default_language,
    )
    await session.commit()

    logger.info(
        "child_node_created",
        node_id=str(node.id),
        parent_materialnode_id=str(node_id),
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
) -> NodeWithMaterialsResponse:
    """Get the full subtree with materials attached to each node.

    Returns the hierarchical view including materials at each level
    and their lifecycle states. Includes a course-level fingerprint.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = MaterialNodeRepository(session)
    tree_roots = await repo.get_subtree(node_id, include_materials=True)
    if not tree_roots:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeWithMaterialsResponse.model_validate(tree_roots[0])


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
    """Update a node's title, description, and/or default language.

    Only fields present in the request body are updated.
    To clear a field, send it as ``null``. Updating
    ``default_language`` is a pure DB write — it does not trigger
    re-ingestion of existing materials.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    # Distinguish "field omitted" from "field set to null"
    update_kwargs: dict[str, str | None] = {}
    if "title" in body.model_fields_set:
        update_kwargs["title"] = body.title
    if "description" in body.model_fields_set:
        update_kwargs["description"] = body.description
    if "default_language" in body.model_fields_set:
        update_kwargs["default_language"] = body.default_language

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

    Cycle detection is enforced. Set ``parent_materialnode_id`` to ``null``
    to make the node a root. Returns 422 if the move would create a cycle.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = MaterialNodeRepository(session)

    # Validate target parent belongs to the same tenant
    if body.parent_materialnode_id is not None:
        await _require_node_for_tenant(
            session, tenant.tenant_id, body.parent_materialnode_id
        )

    try:
        node = await repo.move(node_id, body.parent_materialnode_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()

    logger.info(
        "node_moved",
        node_id=str(node_id),
        new_parent_materialnode_id=str(body.parent_materialnode_id),
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
    s3: S3Dep,
) -> None:
    """Delete a node, all descendants, and their S3 files.

    Collects S3 keys from all material entries in the subtree,
    deletes them from S3, then cascades DB deletion.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    node_repo = MaterialNodeRepository(session)

    # Collect S3 keys before DB cascade removes entries
    subtree = await node_repo.get_subtree(node_id, include_materials=True)
    s3_keys: list[str] = []
    for node in _flatten(subtree):
        for entry in node.materials:
            key = s3.extract_key(entry.source_url)
            if key is not None:
                s3_keys.append(key)

    await node_repo.delete(node_id)
    await session.commit()

    # Clean up S3 after successful DB commit
    for key in s3_keys:
        await s3.delete_object(key)

    logger.info(
        "node_deleted",
        node_id=str(node_id),
        s3_files_cleaned=len(s3_keys),
    )


def _flatten(nodes: Sequence[MaterialNode]) -> list[MaterialNode]:
    """Flatten a tree of nodes into a flat list."""
    result: list[MaterialNode] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(node.children)
    return result
