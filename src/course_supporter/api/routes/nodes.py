"""Material tree node management API endpoints.

Provides CRUD operations for the hierarchical material tree
within a course. Nodes form an adjacency-list tree with arbitrary
depth. Tenant isolation is enforced by verifying course ownership
before accessing any node.

Routes
------
- ``POST   /courses/{id}/nodes``                — Create root node
- ``POST   /courses/{id}/nodes/{nid}/children``  — Create child node
- ``GET    /courses/{id}/nodes/tree``            — Get full tree
- ``GET    /courses/{id}/nodes/{nid}``           — Get single node
- ``PATCH  /courses/{id}/nodes/{nid}``           — Update node
- ``POST   /courses/{id}/nodes/{nid}/move``      — Move node (reparent)
- ``POST   /courses/{id}/nodes/{nid}/reorder``   — Reorder among siblings
- ``DELETE /courses/{id}/nodes/{nid}``           — Delete node (cascade)
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    NodeCreateRequest,
    NodeMoveRequest,
    NodeReorderRequest,
    NodeResponse,
    NodeTreeResponse,
    NodeUpdateRequest,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import CourseRepository

logger = structlog.get_logger()

router = APIRouter(tags=["nodes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]
SharedDep = Annotated[TenantContext, Depends(require_scope("prep", "check"))]


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
    repo: MaterialNodeRepository,
    node_id: uuid.UUID,
    course_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the course.

    Raises:
        HTTPException 404: If the node is not found or
            belongs to a different course.
    """
    node = await repo.get_by_id(node_id)
    if node is None or node.course_id != course_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@router.post("/courses/{course_id}/nodes", status_code=201)
async def create_root_node(
    course_id: uuid.UUID,
    body: NodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a root node in the material tree.

    Root nodes have no parent and appear at the top level of the tree.
    The ``order`` is auto-assigned as the next available position.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    repo = MaterialNodeRepository(session)
    node = await repo.create(
        course_id=course_id,
        title=body.title,
        description=body.description,
    )
    await session.commit()

    logger.info(
        "node_created",
        node_id=str(node.id),
        course_id=str(course_id),
        parent_id=None,
    )
    return NodeResponse.model_validate(node)


@router.post("/courses/{course_id}/nodes/{node_id}/children", status_code=201)
async def create_child_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a child node under an existing parent.

    The child inherits the course from the parent. The ``order``
    is auto-assigned as the next available position among siblings.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    await _require_node(repo, node_id, course_id)

    node = await repo.create(
        course_id=course_id,
        parent_id=node_id,
        title=body.title,
        description=body.description,
    )
    await session.commit()

    logger.info(
        "node_created",
        node_id=str(node.id),
        course_id=str(course_id),
        parent_id=str(node_id),
    )
    return NodeResponse.model_validate(node)


@router.get("/courses/{course_id}/nodes/tree")
async def get_tree(
    course_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[NodeTreeResponse]:
    """Get the full material tree for a course.

    Returns all nodes in a nested structure, with children
    recursively populated. Root nodes (no parent) are at
    the top level. Each level is sorted by ``order``.

    Returns an empty list if the course has no nodes.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    repo = MaterialNodeRepository(session)
    roots = await repo.get_tree(course_id)
    return [NodeTreeResponse.model_validate(r) for r in roots]


@router.get("/courses/{course_id}/nodes/{node_id}")
async def get_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeResponse:
    """Get a single node by ID.

    Returns the flat node representation without children.
    Use ``GET /courses/{id}/nodes/tree`` for the nested tree.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    node = await _require_node(repo, node_id, course_id)
    return NodeResponse.model_validate(node)


@router.patch("/courses/{course_id}/nodes/{node_id}")
async def update_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Update a node's title and/or description.

    Only fields present in the request body are updated.
    To clear the description, send ``"description": null``.
    Omitting a field leaves it unchanged.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    await _require_node(repo, node_id, course_id)

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


@router.post("/courses/{course_id}/nodes/{node_id}/move")
async def move_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeMoveRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Move a node to a new parent (or to root).

    Cycle detection is enforced: a node cannot be moved under
    one of its own descendants. Set ``parent_id`` to ``null``
    to make the node a root.

    Returns 422 if the move would create a cycle or is
    otherwise invalid.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    await _require_node(repo, node_id, course_id)

    # Validate target parent belongs to the same course
    if body.parent_id is not None:
        await _require_node(repo, body.parent_id, course_id)

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


@router.post("/courses/{course_id}/nodes/{node_id}/reorder")
async def reorder_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeReorderRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Reorder a node among its siblings.

    The target ``order`` is 0-based. If the value exceeds the
    number of siblings, it is clamped to the last position.
    All siblings are renumbered sequentially after the operation.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    await _require_node(repo, node_id, course_id)

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


@router.delete("/courses/{course_id}/nodes/{node_id}", status_code=204)
async def delete_node(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Delete a node and all its descendants.

    Deletion cascades to all child nodes and their attached
    material entries. This operation cannot be undone.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    repo = MaterialNodeRepository(session)
    await _require_node(repo, node_id, course_id)

    await repo.delete(node_id)
    await session.commit()

    logger.info(
        "node_deleted",
        node_id=str(node_id),
        course_id=str(course_id),
    )
