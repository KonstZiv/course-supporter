"""Reconciliation preview and apply endpoints.

Routes
------
- ``POST /nodes/{nid}/reconcile/preview`` — Enqueue async preview job
- ``GET  /nodes/{nid}/reconcile/status``  — Preview freshness + job status
- ``GET  /nodes/{nid}/reconcile/preview/result`` — Fetch completed result
- ``POST /nodes/{nid}/reconcile/apply``   — Apply accepted issue fixes
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
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
    ReconciliationStatusResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_reconcile_preview
from course_supporter.fingerprint import FingerprintService, compute_editable_tree_hash
from course_supporter.storage.editable_conversion import _CONTENT_FIELDS
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.reconciliation_preview_repository import (
    ReconciliationPreviewRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["reconciliation"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

# Only content fields may be patched via reconciliation apply.
_ALLOWED_FIELDS: frozenset[str] = frozenset(_CONTENT_FIELDS)


ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


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


def _make_combined_fingerprint(node_fp: str, editable_hash: str) -> str:
    """Build combined fingerprint from node fingerprint and editable hash."""
    return hashlib.sha256(f"{node_fp}:{editable_hash}".encode()).hexdigest()


async def _compute_current_fingerprints(
    session: AsyncSession,
    node_id: uuid.UUID,
) -> tuple[str | None, str | None]:
    """Compute current (node_fingerprint, editable_tree_hash) for a node.

    Returns (None, editable_hash) if node fingerprint cannot be computed
    (e.g. materials not processed). Returns (node_fp, None) if no editable
    tree exists.
    """
    # Node fingerprint — need materials + children loaded
    node_repo = MaterialNodeRepository(session)
    subtree = await node_repo.get_subtree(node_id, include_materials=True)
    if not subtree:
        return None, None

    root = subtree[0]
    node_fp: str | None = None
    try:
        fp_service = FingerprintService(session)
        node_fp = await fp_service.ensure_node_fp(root)
        await session.flush()
    except (ValueError, AttributeError):
        node_fp = None

    # Editable tree hash
    editable_repo = EditableRepository(session)
    flat = await editable_repo.get_tree(node_id)
    editable_hash: str | None = None
    if flat:
        editable_hash = compute_editable_tree_hash(flat)

    return node_fp, editable_hash


def _determine_freshness(
    preview_node_fp: str,
    preview_editable_hash: str,
    current_node_fp: str | None,
    current_editable_hash: str | None,
) -> str:
    """Determine freshness by comparing preview vs current fingerprints."""
    materials_match = current_node_fp is not None and preview_node_fp == current_node_fp
    editable_match = (
        current_editable_hash is not None
        and preview_editable_hash == current_editable_hash
    )

    if materials_match and editable_match:
        return "fresh"
    if not materials_match and not editable_match:
        return "stale_both"
    if not materials_match:
        return "stale_materials"
    return "stale_edited"


@router.get("/nodes/{node_id}/reconcile/status")
async def reconcile_status(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> ReconciliationStatusResponse:
    """Get reconciliation preview status and freshness for a node.

    Returns cached preview (if any), freshness assessment, and
    active job status.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    preview_repo = ReconciliationPreviewRepository(session)
    latest = await preview_repo.get_latest(node_id)

    # Find active reconcile_preview job
    job_repo = JobRepository(session)
    active_jobs = await job_repo.get_active_for_node(node_id)
    recon_job = next(
        (j for j in active_jobs if j.job_type == "reconcile_preview"),
        None,
    )

    if latest is None:
        return ReconciliationStatusResponse(
            has_preview=False,
            preview=None,
            freshness="none",
            job_id=str(recon_job.id) if recon_job else None,
            job_status=recon_job.status if recon_job else None,
        )

    # Compute current fingerprints
    current_node_fp, current_editable_hash = await _compute_current_fingerprints(
        session, node_id
    )

    freshness = _determine_freshness(
        latest.node_fingerprint,
        latest.editable_tree_hash,
        current_node_fp,
        current_editable_hash,
    )

    preview_response = ReconciliationPreviewResponse(
        issues=latest.issues,
        context_summary=latest.context_summary or "",
    )

    return ReconciliationStatusResponse(
        has_preview=True,
        preview=preview_response,
        freshness=freshness,
        job_id=str(recon_job.id) if recon_job else None,
        job_status=recon_job.status if recon_job else None,
    )


@router.post("/nodes/{node_id}/reconcile/preview", status_code=202)
async def reconcile_preview(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> JobResponse:
    """Enqueue async reconciliation preview for cross-node consistency issues.

    Idempotent: if a preview with matching fingerprint already exists,
    returns 200 with the existing job info. Otherwise enqueues a new
    job and returns 202.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = EditableRepository(session)
    flat = await repo.get_tree(node_id)

    if not flat:
        raise HTTPException(
            status_code=404,
            detail="No editable tree found. Generate a structure first.",
        )

    # Compute fingerprints for idempotency
    current_node_fp, current_editable_hash = await _compute_current_fingerprints(
        session, node_id
    )

    if current_node_fp and current_editable_hash:
        combined_fp = _make_combined_fingerprint(current_node_fp, current_editable_hash)

        preview_repo = ReconciliationPreviewRepository(session)
        existing = await preview_repo.find_by_fingerprint(node_id, combined_fp)

        if existing is not None:
            logger.info(
                "reconcile_preview_idempotent",
                node_id=str(node_id),
                combined_fp=combined_fp[:16],
            )
            # Return existing job if available, or a synthetic response
            if existing.job_id:
                job_repo = JobRepository(session)
                job = await job_repo.get_by_id(existing.job_id)
                if job is not None:
                    return JobResponse.model_validate(job)

    fp_to_store = combined_fp if current_node_fp and current_editable_hash else None
    job = await enqueue_reconcile_preview(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
        combined_fingerprint=fp_to_store,
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

    return ReconciliationPreviewResponse.model_validate(job.result_data)


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
