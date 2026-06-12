"""Methodist node-summary HTTP surface (Phase 3.2.4 c2).

Six endpoints that close the methodist backend layer:

* ``POST /api/v1/nodes/{node_id}/summary/generate`` — trigger the
  two-pass methodist regeneration via ARQ.
* ``GET  /api/v1/nodes/{node_id}/summary`` — P6 GET (§1910 two-step
  contract: generic 404 on missing/foreign + 404 ``not_yet_generated``
  on missing Raw + 200 on present Raw).
* ``PATCH /api/v1/node-summaries/{node_id}/final`` — channel 2 (author
  manual edit), editable family per KD11 §1043-1054.
* ``POST /api/v1/node-summaries/{node_id}/final/approve`` — explicit
  approve + snapshot hard-delete.
* ``POST /api/v1/node-summaries/{node_id}/final/accept-raw`` — implicit
  approve flavour of channel 1 + snapshot hard-delete.
* ``GET  /api/v1/node-summaries/{node_id}/edit-view`` — combined view
  (Final + raw_observations + previous_snapshot) per KD11 §1086-1101.

Invariants enforced by this module:

* Tenant-gate (rule #12) — every endpoint uses
  :func:`_require_node_for_tenant` to render foreign-tenant /
  nonexistent-node into the same byte-identical generic 404.
* Inv #4 (TZ) — routes never invoke the orchestrator synchronously;
  ``POST .../generate`` enqueues an ARQ job and returns immediately
  with the queued ``JobResponse``.
* Inv #1 (TZ) — all writes into ``node_summaries_final*`` go through
  :class:`NodeSummaryFinalRepository`; routes do NOT execute direct
  SQL on those tables.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.methodist_factory import (
    build_node_summary_orchestrator,
)
from course_supporter.api.deps import get_arq_redis, get_session, get_stage_router
from course_supporter.api.schemas import (
    JobResponse,
    NodeSummaryEditViewResponse,
    NodeSummaryFinalResponse,
    NodeSummaryFinalUpdateRequest,
    NodeSummaryGenerateRequest,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_node_summary_regeneration
from course_supporter.llm.stage_router import StageRouter
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.node_summary_final_repository import (
    NodeSummaryFinalRepository,
)
from course_supporter.storage.orm import NodeSummaryRaw

logger = structlog.get_logger()

router = APIRouter(tags=["node-summaries"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
StageRouterDep = Annotated[StageRouter, Depends(get_stage_router)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


# ── tenant-gate helpers ───────────────────────────────────────────


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> None:
    """Generic 404 on missing-or-foreign CourseNode (rule #12).

    Mirrors :func:`api.routes.nodes._require_node_for_tenant` byte-for-
    byte (same ``detail`` string) so foreign-tenant and nonexistent
    nodes are observably indistinguishable.
    """
    repo = CourseNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")


async def _fetch_raw_for_node(
    session: AsyncSession, course_node_id: uuid.UUID
) -> NodeSummaryRaw | None:
    """Read the active Raw row for the node; ``None`` if not yet generated."""
    stmt = select(NodeSummaryRaw).where(
        NodeSummaryRaw.course_node_id == course_node_id,
        NodeSummaryRaw.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ── POST /nodes/{node_id}/summary/generate ────────────────────────


@router.post("/nodes/{node_id}/summary/generate", status_code=202)
async def generate_node_summary(
    node_id: uuid.UUID,
    body: NodeSummaryGenerateRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
    stage_router: StageRouterDep,
) -> JobResponse:
    """Trigger the methodist two-pass regeneration via ARQ (KD10 + KD13).

    Two-step path:

    1. **Read-only scope validation (K1 ratify — the 422 API surface).**
       Constructs the orchestrator via the shared
       :func:`build_node_summary_orchestrator` DI helper and calls
       :meth:`validate_scope` (purely read-only — selects + hash
       comparison; no LLM hook invoked). When the run would leave
       ``uncovered_stale_node_ids`` outside the vertex's subtree and
       ``force = false``, returns 422 with the list of uncovered ids
       so the caller can either narrow scope or retry with
       ``force = true``.
    2. **Enqueue.** Mirrors ``enqueue_ingestion``: persists a
       ``node_summary_regeneration`` Job, enqueues the ARQ task,
       records ``arq_job_id``, commits, returns the queued Job.

    Inv #4 (clarified): routes never call the orchestrator's
    **generation** (``orch.run()``); the read-only ``validate_scope``
    is the explicit K1-ratified 422 surface and IS permitted here.
    The orchestrator's session is the route's request-scoped session,
    so the scope check participates in the same transaction view as
    the subsequent enqueue.

    Errors:
    * 404 generic — node missing or owned by another tenant.
    * 422 — ``uncovered_stale_node_ids`` non-empty and ``force=false``.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    orch = build_node_summary_orchestrator(session, stage_router)
    scope = await orch.validate_scope(node_id, body.force)
    if scope.uncovered_stale_node_ids and not body.force:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "uncovered_stale_nodes",
                "uncovered_stale_node_ids": [
                    str(nid) for nid in scope.uncovered_stale_node_ids
                ],
                "hint": (
                    "Stale CourseNodes exist outside the requested "
                    "vertex's subtree. Retry with force=true to "
                    "proceed regardless, or move the vertex up the "
                    "tree so the stale ancestors fall in scope."
                ),
            },
        )

    job = await enqueue_node_summary_regeneration(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        vertex_node_id=node_id,
        force=body.force,
    )
    await session.commit()

    logger.info(
        "node_summary_regeneration_enqueued",
        job_id=str(job.id),
        node_id=str(node_id),
        force=body.force,
        uncovered_stale_count=len(scope.uncovered_stale_node_ids),
    )
    return JobResponse.model_validate(job)


# ── GET /nodes/{node_id}/summary (P6 two-step contract) ──────────


@router.get("/nodes/{node_id}/summary")
async def get_node_summary(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeSummaryFinalResponse:
    """P6 GET — two-step contract per §1910.

    * Step 1: foreign tenant or missing node → generic 404
      ``Node not found`` (rule #12).
    * Step 2: node found, Raw absent → 404 ``not_yet_generated``.
    * Step 3: Raw present → 200 with the full Final contract.

    Gate is purely Raw-existence (operator-ratified Phase 3.2.4
    pre-flight question (b)). ``is_manual`` empty-leaf-without-Raw
    branch is a forward-compat dead path in 3.2.4 backend (no API
    creates such Finals).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    raw = await _fetch_raw_for_node(session, node_id)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    repo = NodeSummaryFinalRepository(session)
    final = await repo.get_by_course_node_id(node_id)
    if final is None:
        # Defensive: in current orchestrator wiring channel 1 fires at
        # Pass 1 so Raw and Final exist together. Surfacing the same
        # ``not_yet_generated`` keeps the client contract simple.
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )
    return NodeSummaryFinalResponse.model_validate(final)


# ── PATCH /node-summaries/{node_id}/final (channel 2) ────────────


@router.patch("/node-summaries/{node_id}/final")
async def patch_node_summary_final(
    node_id: uuid.UUID,
    body: NodeSummaryFinalUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeSummaryFinalResponse:
    """Channel 2 — author manual edit of the KD11 §1043-1054 family.

    Two-level validation:

    1. **Request schema (first line of defence).** Pydantic's
       ``extra='forbid'`` on :class:`NodeSummaryFinalUpdateRequest`
       rejects non-editable keys with 422 at the boundary
       (``main_concepts`` / ``enclosing_context`` / ``content_hash`` /
       any meta field / ``is_manual`` / ``manual_description``).
    2. **Repository ``ValueError`` (deep gate).** Belt-and-suspenders
       guard against a future schema relaxation; surfaces as 422 here.

    ``approved_at`` and ``is_manual`` are NOT touched (KD11: edits do
    not auto-invalidate approvals; ``is_manual`` is forward-compat
    only).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status_code=422,
            detail="No editable fields supplied in PATCH body.",
        )

    repo = NodeSummaryFinalRepository(session)
    try:
        final = await repo.patch_editable(node_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if final is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    await session.commit()
    return NodeSummaryFinalResponse.model_validate(final)


# ── POST /node-summaries/{node_id}/final/approve ──────────────────


@router.post("/node-summaries/{node_id}/final/approve")
async def approve_node_summary_final(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeSummaryFinalResponse:
    """Explicit approve — sets ``approved_at = now()`` + hard-delete snapshot."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = NodeSummaryFinalRepository(session)
    final = await repo.approve(node_id)
    if final is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    await session.commit()
    return NodeSummaryFinalResponse.model_validate(final)


# ── POST /node-summaries/{node_id}/final/accept-raw ───────────────


@router.post("/node-summaries/{node_id}/final/accept-raw")
async def accept_raw_node_summary_final(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeSummaryFinalResponse:
    """Implicit-approve flavour of channel 1: overwrite Final from Raw + approve."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    raw = await _fetch_raw_for_node(session, node_id)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    repo = NodeSummaryFinalRepository(session)
    final = await repo.accept_raw(raw)
    if final is None:
        # Defensive — Raw present without Final cannot happen in 3.2.4
        # backend (channel 1 creates Final at Pass 1 together with Raw),
        # but the route stays defensive.
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    await session.commit()
    return NodeSummaryFinalResponse.model_validate(final)


# ── GET /node-summaries/{node_id}/edit-view ───────────────────────


@router.get("/node-summaries/{node_id}/edit-view")
async def get_node_summary_edit_view(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeSummaryEditViewResponse:
    """Combined edit-view per KD11 §1086-1101 (final + raw_observations + snapshot)."""
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = NodeSummaryFinalRepository(session)
    view = await repo.get_edit_view(node_id)
    if view is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "not_yet_generated"},
        )

    final, raw_observations, snapshot = view
    return NodeSummaryEditViewResponse(
        final=NodeSummaryFinalResponse.model_validate(final),
        raw_observations=raw_observations,
        previous_snapshot=snapshot.snapshot if snapshot is not None else None,
    )
