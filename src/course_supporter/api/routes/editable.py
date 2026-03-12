"""Editable structure node API endpoints.

Provides endpoints for reading and updating the mutable working copy
of course structure nodes.

Routes
------
- ``GET   /nodes/{nid}/editable``              — Full editable tree
- ``PATCH /nodes/{nid}/editable/{eid}``        — Update editable fields
- ``POST  /nodes/{nid}/editable/init``         — Re-init from snapshot
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    EditableInitRequest,
    EditableNodeResponse,
    EditableNodeUpdateRequest,
    EditableTreeResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import StructureNodeEditable
from course_supporter.storage.snapshot_repository import SnapshotRepository

logger = structlog.get_logger()

router = APIRouter(tags=["editable"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


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


def _orm_to_response(node: StructureNodeEditable) -> EditableNodeResponse:
    """Convert ORM object to response, bypassing lazy-loaded relationships."""
    data = {
        k: getattr(node, k)
        for k in EditableNodeResponse.model_fields
        if k != "children"
    }
    return EditableNodeResponse.model_validate({**data, "children": []})


def _build_tree(flat: list[StructureNodeEditable]) -> list[EditableNodeResponse]:
    """Convert flat list of editables into a nested tree of responses."""
    response_map: dict[uuid.UUID, EditableNodeResponse] = {}
    roots: list[EditableNodeResponse] = []

    for node in flat:
        resp = _orm_to_response(node)
        response_map[node.id] = resp

    for node in flat:
        resp = response_map[node.id]
        parent_id = node.parent_editable_id
        if parent_id is not None and parent_id in response_map:
            response_map[parent_id].children.append(resp)
        else:
            roots.append(resp)

    return roots


@router.get("/nodes/{node_id}/editable")
async def get_editable_tree(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> EditableTreeResponse:
    """Get the full editable structure tree for a material node."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = EditableRepository(session)
    flat = await repo.get_tree(node_id)

    if not flat:
        raise HTTPException(
            status_code=404,
            detail="No editable tree found. Generate a structure first.",
        )

    source_snapshot_id = flat[0].source_snapshot_id if flat else None

    return EditableTreeResponse(
        materialnode_id=node_id,
        source_snapshot_id=source_snapshot_id,
        nodes=_build_tree(flat),
    )


@router.patch("/nodes/{node_id}/editable/{editable_id}")
async def update_editable_node(
    node_id: uuid.UUID,
    editable_id: uuid.UUID,
    body: EditableNodeUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> EditableNodeResponse:
    """Update specific fields of an editable structure node."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = EditableRepository(session)
    node = await repo.get_by_id(editable_id)
    if node is None or node.materialnode_id != node_id:
        raise HTTPException(status_code=404, detail="Editable node not found")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")

    updated = await repo.update_fields(editable_id, fields)
    await session.commit()
    await session.refresh(updated)

    return _orm_to_response(updated)


@router.post("/nodes/{node_id}/editable/init")
async def init_editable_tree(
    node_id: uuid.UUID,
    body: EditableInitRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> EditableTreeResponse:
    """(Re-)initialise editable tree from a snapshot."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # Resolve snapshot
    snapshot_id = body.snapshot_id
    if snapshot_id is None:
        snap_repo = SnapshotRepository(session)
        latest = await snap_repo.get_latest_for_node(node_id)
        if latest is None:
            raise HTTPException(
                status_code=404,
                detail="No snapshot found for this node.",
            )
        snapshot_id = latest.id

    repo = EditableRepository(session)
    editables = await repo.init_from_snapshot(
        snapshot_id=snapshot_id,
        materialnode_id=node_id,
        preserve_edited=body.preserve_edited,
    )
    await session.commit()

    # Re-fetch the tree after commit to avoid MissingGreenlet on expired ORM objects
    editables = await repo.get_tree(node_id)

    source_snapshot_id = editables[0].source_snapshot_id if editables else None

    return EditableTreeResponse(
        materialnode_id=node_id,
        source_snapshot_id=source_snapshot_id,
        nodes=_build_tree(editables),
    )
