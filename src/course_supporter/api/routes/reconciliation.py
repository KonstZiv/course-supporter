"""Reconciliation preview and apply endpoints.

Routes
------
- ``POST /nodes/{nid}/reconcile/preview`` — Enqueue async preview job
- ``POST /nodes/{nid}/reconcile/apply``   — Apply accepted issue fixes
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.routes.editable import (
    _build_tree,
    _orm_to_response,
    _require_node_for_tenant,
)
from course_supporter.api.schemas import (
    EditableTreeResponse,
    JobResponse,
    ReconcileApplyRequest,
    ReconciliationPreviewResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_reconcile_preview
from course_supporter.storage.editable_conversion import _CONTENT_FIELDS
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.job_repository import JobRepository

logger = structlog.get_logger()

router = APIRouter(tags=["reconciliation"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

# Only content fields may be patched via reconciliation apply.
_ALLOWED_FIELDS: frozenset[str] = frozenset(_CONTENT_FIELDS)


def _get_arq(request: Request) -> ArqRedis:
    """Extract ARQ Redis pool from app state."""
    arq_redis: ArqRedis = request.app.state.arq_redis
    return arq_redis


ArqDep = Annotated[ArqRedis, Depends(_get_arq)]


def _editable_tree_to_dicts(
    flat: list[Any],
) -> list[dict[str, Any]]:
    """Convert flat editable ORM list to nested dicts for LLM context."""
    response_map: dict[uuid.UUID, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    for node in flat:
        resp = _orm_to_response(node)
        d = resp.model_dump(mode="json")
        d["children"] = []
        response_map[node.id] = d

    for node in flat:
        d = response_map[node.id]
        parent_id = node.parent_editable_id
        if parent_id is not None and parent_id in response_map:
            response_map[parent_id]["children"].append(d)
        else:
            roots.append(d)

    return roots


@router.post("/nodes/{node_id}/reconcile/preview", status_code=202)
async def reconcile_preview(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> JobResponse:
    """Enqueue async reconciliation preview for cross-node consistency issues.

    Returns a Job record (202). Poll ``GET /jobs/{job_id}`` for status.
    When complete, ``result_data`` contains the preview issues.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = EditableRepository(session)
    flat = await repo.get_tree(node_id)

    if not flat:
        raise HTTPException(
            status_code=404,
            detail="No editable tree found. Generate a structure first.",
        )

    job = await enqueue_reconcile_preview(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
    )
    await session.commit()

    return JobResponse.model_validate(job)


@router.get("/nodes/{node_id}/reconcile/preview/result")
async def reconcile_preview_result(
    node_id: uuid.UUID,
    job_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> ReconciliationPreviewResponse:
    """Fetch reconciliation preview result from a completed job.

    Query params:
        job_id: UUID of the reconcile_preview job.

    Returns 404 if job not found, 409 if job not yet complete.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    job_repo = JobRepository(session)
    job = await job_repo.get_by_id_for_tenant(job_id, tenant.tenant_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.job_type != "reconcile_preview":
        raise HTTPException(status_code=400, detail="Job is not a reconcile_preview")

    if job.status != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"Job not yet complete (status: {job.status})",
        )

    if not job.result_data:
        raise HTTPException(
            status_code=500,
            detail="Job completed but result_data is missing",
        )

    return ReconciliationPreviewResponse(
        issues=job.result_data.get("issues", []),
        context_summary=job.result_data.get("context_summary", ""),
    )


@router.post("/nodes/{node_id}/reconcile/apply")
async def reconcile_apply(
    node_id: uuid.UUID,
    body: ReconcileApplyRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> EditableTreeResponse:
    """Apply accepted reconciliation issues to editable nodes.

    Patches each accepted issue's field with the suggested value.
    Does NOT add to ``edited_fields`` — these are LLM-suggested changes.
    Only fields in ``_CONTENT_FIELDS`` whitelist are allowed.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    accepted_ids = set(body.accepted_issue_ids)
    accepted_issues = [i for i in body.issues if i.id in accepted_ids]

    if not accepted_issues:
        raise HTTPException(status_code=422, detail="No accepted issues to apply")

    repo = EditableRepository(session)

    # Batch-load all referenced nodes in one query
    flat = await repo.get_tree(node_id)
    nodes_map = {n.id: n for n in flat}

    for issue in accepted_issues:
        if issue.field not in _ALLOWED_FIELDS:
            logger.warning(
                "reconcile_apply_skip_field",
                editable_id=str(issue.editable_node_id),
                field=issue.field,
                reason="field_not_in_whitelist",
            )
            continue

        node = nodes_map.get(issue.editable_node_id)
        if node is None:
            logger.warning(
                "reconcile_apply_skip_node",
                editable_id=str(issue.editable_node_id),
                reason="not_found_in_tree",
            )
            continue

        setattr(node, issue.field, issue.suggested_value)

    await session.flush()
    await session.commit()

    flat = await repo.get_tree(node_id)
    source_snapshot_id = flat[0].source_snapshot_id if flat else None

    return EditableTreeResponse(
        materialnode_id=node_id,
        source_snapshot_id=source_snapshot_id,
        nodes=_build_tree(flat),
    )
