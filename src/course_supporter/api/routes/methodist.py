"""Methodist agent API endpoints.

Provides endpoints for triggering Methodist analysis on a course
structure and retrieving results.

Routes
------
- ``POST /nodes/{nid}/methodist`` — Trigger Methodist for a subtree
- ``GET  /nodes/{nid}/methodist`` — Get Methodist results
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
    JobResponse,
    MethodistNodeResponse,
    MethodistPlanResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.material_node_repository import (
    MaterialNodeRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["methodist"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext,
    Depends(require_scope(AuthScope.PREP, AuthScope.CHECK)),
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


@router.post("/nodes/{node_id}/methodist", status_code=202)
async def trigger_methodist(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> MethodistPlanResponse:
    """Trigger Methodist analysis for a node's editable tree.

    Requires that structure generation has been run first
    (editable tree must exist). Returns 202 if work was enqueued,
    404 if node not found, 422 if no editable tree exists.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # Check editable tree exists
    editable_repo = EditableRepository(session)
    editables = await editable_repo.get_tree(node_id)
    if not editables:
        raise HTTPException(
            status_code=422,
            detail=("No editable tree found. Run structure generation first."),
        )

    from course_supporter.methodist_orchestrator import (
        trigger_methodist as _trigger,
    )

    try:
        plan = await _trigger(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            materialnode_id=node_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    await session.commit()

    logger.info(
        "methodist_triggered",
        node_id=str(node_id),
        bottom_up_count=len(plan.bottom_up_jobs),
        top_down_count=len(plan.top_down_jobs),
        estimated_llm_calls=plan.estimated_llm_calls,
    )

    return MethodistPlanResponse(
        bottom_up_jobs=[JobResponse.model_validate(j) for j in plan.bottom_up_jobs],
        top_down_jobs=[JobResponse.model_validate(j) for j in plan.top_down_jobs],
        estimated_llm_calls=plan.estimated_llm_calls,
    )


@router.get("/nodes/{node_id}/methodist")
async def get_methodist_results(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[MethodistNodeResponse]:
    """Get Methodist results for a node's editable tree.

    Returns all editable nodes with their Methodist output
    (or null if not yet processed).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    editable_repo = EditableRepository(session)
    editables = await editable_repo.get_tree(node_id)

    if not editables:
        raise HTTPException(
            status_code=404,
            detail="No editable tree found.",
        )

    return [
        MethodistNodeResponse(
            editable_id=ed.id,
            node_type=ed.node_type,
            title=ed.title,
            methodological_content=ed.methodological_content,
            methodological_markdown=ed.methodological_markdown,
            has_methodist_output=ed.methodological_content is not None,
        )
        for ed in editables
    ]
