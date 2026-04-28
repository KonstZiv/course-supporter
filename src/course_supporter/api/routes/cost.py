"""Cost reporting API endpoints (vision §3 KD5).

Two read-only endpoints scoped to the calling tenant via the API key:

* ``GET /api/v1/cost/summary`` — period total + by-course + by-provider.
* ``GET /api/v1/cost/course/{course_node_id}`` — drill-down into a
  single course (subtree) with by-node + by-action.

Both responses are cached in Redis for 5 minutes (
:data:`cost_cache.TTL_SECONDS`); ``?no_cache=true`` bypasses both read
and write paths (admin/debug helper, hidden from OpenAPI).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.models.cost import (
    ByActionEntry,
    ByCourseEntry,
    ByNodeEntry,
    ByProviderEntry,
    CostSummaryResponse,
    CourseCostResponse,
)
from course_supporter.services.cost_cache import (
    get_cached,
    make_course_key,
    make_summary_key,
    set_cached,
)
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import ExternalServiceCallRepository

logger = structlog.get_logger()

router = APIRouter(tags=["cost"])

SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


@router.get("/cost/summary", response_model=CostSummaryResponse)
async def get_cost_summary(
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[ArqRedis, Depends(get_arq_redis)],
    from_: Annotated[date, Query(alias="from")],
    to_: Annotated[date, Query(alias="to")],
    limit_courses: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_courses: Annotated[int, Query(ge=0)] = 0,
    limit_providers: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_providers: Annotated[int, Query(ge=0)] = 0,
    no_cache: Annotated[bool, Query(include_in_schema=False)] = False,
) -> CostSummaryResponse:
    """Tenant-wide cost summary for the requested date range.

    The by_course breakdown groups by direct ``Job.course_node_id``
    (Variant D per KD5 §5 row 0.4 deliberation). Jobs targeting
    non-root nodes appear as their own rows; UI clients aggregate at
    course-root level. Phase 1+ task may evaluate walk-up CTE or
    schema denormalization (``MaterialNode.root_course_id``) based on
    production query latency.

    Security: ``?no_cache=true`` always hits the DB. Absolute frequency
    is bounded by the ``require_scope(PREP, CHECK)`` rate limit per
    plan (so a misbehaving caller cannot exceed their plan's QPS), but
    every bypassed cache hit multiplies DB load relative to the same
    rate of cached requests. Each bypass emits a WARNING log
    (``cost_no_cache_bypass``) for audit. A separate, tighter
    rate-limit bucket for ``no_cache=true`` traffic is captured as a
    Phase 1+ follow-up — natural fit alongside the admin-scope work
    that KD5 defers.
    """
    if from_ > to_:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if no_cache:
        logger.warning(
            "cost_no_cache_bypass",
            tenant_id=str(tenant.tenant_id),
            endpoint="/cost/summary",
            from_=str(from_),
            to_=str(to_),
        )

    cache_key = make_summary_key(
        tenant_id=tenant.tenant_id,
        from_date=from_,
        to_date=to_,
        limit_courses=limit_courses,
        offset_courses=offset_courses,
        limit_providers=limit_providers,
        offset_providers=offset_providers,
    )
    if not no_cache:
        cached = await get_cached(redis, cache_key)
        if cached is not None:
            return CostSummaryResponse.model_validate_json(cached)

    repo = ExternalServiceCallRepository(session)
    total = await repo.get_total_for_period(
        tenant_id=tenant.tenant_id, from_date=from_, to_date=to_
    )
    unattributed = await repo.get_unattributed_for_period(
        tenant_id=tenant.tenant_id, from_date=from_, to_date=to_
    )
    by_course_rows = await repo.get_by_course(
        tenant_id=tenant.tenant_id,
        from_date=from_,
        to_date=to_,
        limit=limit_courses,
        offset=offset_courses,
    )
    by_provider_rows = await repo.get_by_provider(
        tenant_id=tenant.tenant_id,
        from_date=from_,
        to_date=to_,
        limit=limit_providers,
        offset=offset_providers,
    )

    # KD5 invariant: total_usd == sum(by_course.cost_usd) +
    #                              unattributed_cost_usd.
    # The breakdown lists may be paginated — total stays whole.
    response = CostSummaryResponse(
        from_=from_,
        to_=to_,
        total_usd=total,
        unattributed_cost_usd=unattributed,
        by_course=[
            ByCourseEntry(
                course_node_id=row.course_node_id,
                course_title=row.course_title,
                cost_usd=row.cost_usd,
            )
            for row in by_course_rows
        ],
        by_provider=[
            ByProviderEntry(
                provider=row.provider,
                model_id=row.model_id,
                cost_usd=row.cost_usd,
            )
            for row in by_provider_rows
        ],
    )

    if not no_cache:
        await set_cached(redis, cache_key, response.model_dump_json(by_alias=True))
    return response


@router.get("/cost/course/{course_node_id}", response_model=CourseCostResponse)
async def get_cost_course(
    course_node_id: uuid.UUID,
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[ArqRedis, Depends(get_arq_redis)],
    from_: Annotated[date, Query(alias="from")],
    to_: Annotated[date, Query(alias="to")],
    limit_nodes: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_nodes: Annotated[int, Query(ge=0)] = 0,
    limit_actions: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_actions: Annotated[int, Query(ge=0)] = 0,
    no_cache: Annotated[bool, Query(include_in_schema=False)] = False,
) -> CourseCostResponse:
    """Cost drill-down for a single course (subtree drill-down).

    Security: ``?no_cache=true`` always hits the DB and additionally
    re-runs the recursive descendant CTE in ``get_descendant_ids``.
    Same rate-limit boundedness + audit-log story as ``/cost/summary``;
    see that endpoint's docstring.
    """
    if from_ > to_:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if no_cache:
        logger.warning(
            "cost_no_cache_bypass",
            tenant_id=str(tenant.tenant_id),
            endpoint="/cost/course",
            course_node_id=str(course_node_id),
            from_=str(from_),
            to_=str(to_),
        )

    node_repo = MaterialNodeRepository(session)
    # Tenant isolation: 404 covers both non-existent IDs AND foreign-tenant
    # IDs to avoid leaking existence across tenants.
    course = await node_repo.get_by_id_for_tenant(course_node_id, tenant.tenant_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    cache_key = make_course_key(
        tenant_id=tenant.tenant_id,
        course_node_id=course_node_id,
        from_date=from_,
        to_date=to_,
        limit_nodes=limit_nodes,
        offset_nodes=offset_nodes,
        limit_actions=limit_actions,
        offset_actions=offset_actions,
    )
    if not no_cache:
        cached = await get_cached(redis, cache_key)
        if cached is not None:
            return CourseCostResponse.model_validate_json(cached)

    # Empty descendant_ids → response with total_usd=0 + empty breakdowns
    # ("course exists but no Jobs in period"). Distinct from 404 above.
    # tenant_id passed through as a fourth defense layer — even if a data
    # anomaly placed a foreign-tenant node under this subtree, recursion
    # won't traverse into it.
    descendant_ids = await node_repo.get_descendant_ids(
        course_node_id, tenant_id=tenant.tenant_id
    )

    repo = ExternalServiceCallRepository(session)
    total = await repo.get_total_for_subtree(
        tenant_id=tenant.tenant_id,
        course_node_ids=descendant_ids,
        from_date=from_,
        to_date=to_,
    )
    by_node_rows = await repo.get_by_node(
        tenant_id=tenant.tenant_id,
        course_node_ids=descendant_ids,
        from_date=from_,
        to_date=to_,
        limit=limit_nodes,
        offset=offset_nodes,
    )
    by_action_rows = await repo.get_by_action(
        tenant_id=tenant.tenant_id,
        course_node_ids=descendant_ids,
        from_date=from_,
        to_date=to_,
        limit=limit_actions,
        offset=offset_actions,
    )

    # KD5 invariant: total_usd == sum(by_node.cost_usd) when by_node is
    # not paginated. With pagination, only the first page sums to total
    # if the course has fewer than limit_nodes attributed nodes.
    response = CourseCostResponse(
        course_node_id=course_node_id,
        course_title=course.title,
        from_=from_,
        to_=to_,
        total_usd=total,
        by_node=[
            ByNodeEntry(
                course_node_id=row.course_node_id,
                title=row.title,
                cost_usd=row.cost_usd,
            )
            for row in by_node_rows
        ],
        by_action=[
            ByActionEntry(action=row.action, cost_usd=row.cost_usd)
            for row in by_action_rows
        ],
    )

    if not no_cache:
        await set_cached(redis, cache_key, response.model_dump_json(by_alias=True))
    return response
