"""Cost reporting API endpoints (vision §3 KD5).

Two read-only endpoints scoped to the calling tenant via the API key:

* ``GET /api/v1/cost/summary`` — period total + by-course + by-provider.
* ``GET /api/v1/cost/course/{course_node_id}`` — drill-down into a
  single course (subtree) with by-node + by-action.

Both responses are cached in Redis for 5 minutes (
:data:`cost_cache.TTL_SECONDS`); ``?no_cache=true`` bypasses both read
and write paths (admin/debug helper, hidden from OpenAPI).

## Optional date range (0.7)

``from`` / ``to`` are optional. When omitted, lower bound resolves to
``Tenant.created_at`` (cast to ``date``) and upper bound to
``datetime.now(UTC).date()``. Resolution happens in-handler via the
existing session dependency, *not* by extending ``TenantContext``: the
fallback is needed only on these two cost endpoints, so a one-row
lookup keeps the auth context lean (KD-B: YAGNI). Cache keys carry
the resolved values so a "no params" request collides with the
explicit-dates request that produces the same bounds (KD-F).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis
from course_supporter.api.routes._portal_shared import material_label
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
    HomeworkByCourseEntry,
    HomeworkByStudentEntry,
    HomeworkByTaskEntry,
    HomeworkCostResponse,
    HomeworkCourseCostResponse,
    HomeworkTaskCostResponse,
)
from course_supporter.services.cost_cache import (
    get_cached,
    make_course_key,
    make_summary_key,
    set_cached,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import Tenant
from course_supporter.storage.repositories import (
    ExternalServiceCallRepository,
    HomeworkCostRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["cost"])

SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


async def _resolve_date_range(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_: date | None,
    to_: date | None,
) -> tuple[date, date]:
    """Resolve optional ``from``/``to`` to concrete date bounds.

    * ``from_ is None`` → load ``Tenant`` by id, cast ``created_at`` to
      ``date`` (``Tenant.created_at`` is ``DateTime(timezone=True)``;
      day precision is enough — the lower bound includes the entire
      day the tenant was created).
    * ``to_ is None`` → ``datetime.now(UTC).date()``. UTC matches the
      project-wide ``datetime.now(UTC)`` convention used by every
      other timestamp callsite (``api/deps.py``, ``api/app.py``,
      ``storage/*_repository.py``, etc.); ``date.today()`` would
      silently bind to local timezone.

    The tenant lookup never returns ``None`` in practice because the
    caller's ``TenantContext`` was already authenticated against the
    same DB. The ``assert`` narrows the type for ``mypy --strict``
    rather than papering over a real failure mode.
    """
    if from_ is None:
        tenant_record = await session.get(Tenant, tenant_id)
        assert tenant_record is not None, (
            "Tenant must exist when TenantContext was authenticated against the same DB"
        )
        from_ = tenant_record.created_at.date()
    if to_ is None:
        to_ = datetime.now(UTC).date()
    return from_, to_


@router.get("/cost/summary", response_model=CostSummaryResponse)
async def get_cost_summary(
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[ArqRedis, Depends(get_arq_redis)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    limit_courses: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_courses: Annotated[int, Query(ge=0)] = 0,
    limit_providers: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_providers: Annotated[int, Query(ge=0)] = 0,
    no_cache: Annotated[bool, Query(include_in_schema=False)] = False,
) -> CostSummaryResponse:
    """Tenant-wide cost summary for the requested date range.

    ``from``/``to`` are optional — see module docstring for fallback
    semantics. The ``from > to`` guard runs on resolved values so the
    ordering invariant survives both omitted-params and explicit-params
    callers identically.

    The by_course breakdown groups by direct ``Job.course_node_id``
    (Variant D per KD5 §5 row 0.4 deliberation). Jobs targeting
    non-root nodes appear as their own rows; UI clients aggregate at
    course-root level. Phase 1+ task may evaluate walk-up CTE or
    schema denormalization (``CourseNode.root_course_id``) based on
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
    from_, to_ = await _resolve_date_range(
        session=session,
        tenant_id=tenant.tenant_id,
        from_=from_,
        to_=to_,
    )
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


# ──────────────────────────────────────────────
# Homework cost-attribution (Phase 6, 6.HC)
# ──────────────────────────────────────────────
#
# A separate surface from /cost/summary. Homework ExternalServiceCall rows are
# written by the same _persist surface, but the homework Job carries
# course_node_id=NULL (Variant B rejected, DD-6-I), so /cost/summary files
# their cost under unattributed_cost_usd. These three endpoints attribute it by
# joining ESC straight to HomeworkSubmission on job_id (HomeworkCostRepository).
#
# The tree is drilled on demand: total → by-course → by-task → by-student, each
# level a separate request keyed by the parent. No Redis cache (MVP): freshness
# beats a 5-min TTL on a report drilled interactively. Tenant isolation is
# enforced in the repository on HomeworkSubmission.tenant_id (NOT NULL); the
# course/task path params are scoped in-query, so a foreign or unknown id
# returns an empty breakdown (leak-safe — indistinguishable from an own id with
# no homework), not a 404.


@router.get("/cost/homework", response_model=HomeworkCostResponse)
async def get_homework_cost(
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    limit_courses: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_courses: Annotated[int, Query(ge=0)] = 0,
) -> HomeworkCostResponse:
    """Homework-processing cost for the tenant — total + by-course (level 1).

    ``from``/``to`` are optional with the same fallback semantics as
    ``/cost/summary`` (see module docstring). Drill into a course via each
    ``by_course`` row's ``course_node_id``.
    """
    from_, to_ = await _resolve_date_range(
        session=session,
        tenant_id=tenant.tenant_id,
        from_=from_,
        to_=to_,
    )
    if from_ > to_:
        raise HTTPException(status_code=422, detail="from must be <= to")

    repo = HomeworkCostRepository(session)
    total = await repo.get_homework_total(
        tenant_id=tenant.tenant_id, from_date=from_, to_date=to_
    )
    by_course_rows = await repo.get_homework_by_course(
        tenant_id=tenant.tenant_id,
        from_date=from_,
        to_date=to_,
        limit=limit_courses,
        offset=offset_courses,
    )
    return HomeworkCostResponse(
        from_=from_,
        to_=to_,
        total_usd=total,
        by_course=[
            HomeworkByCourseEntry(
                course_node_id=row.course_node_id,
                course_title=row.course_title,
                cost_usd=row.cost_usd,
            )
            for row in by_course_rows
        ],
    )


@router.get(
    "/cost/homework/course/{course_node_id}",
    response_model=HomeworkCourseCostResponse,
)
async def get_homework_cost_course(
    course_node_id: uuid.UUID,
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    limit_tasks: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_tasks: Annotated[int, Query(ge=0)] = 0,
) -> HomeworkCourseCostResponse:
    """Homework cost for one course, grouped by task (level 2).

    Drilled by the parent level's ``course_node_id``. Soft-deleted tasks appear
    with ``is_deleted=true`` (shown, not filtered). A foreign or unknown course
    returns an empty ``by_task`` (leak-safe — see the section note above).
    """
    from_, to_ = await _resolve_date_range(
        session=session,
        tenant_id=tenant.tenant_id,
        from_=from_,
        to_=to_,
    )
    if from_ > to_:
        raise HTTPException(status_code=422, detail="from must be <= to")

    repo = HomeworkCostRepository(session)
    by_task_rows = await repo.get_homework_by_task(
        tenant_id=tenant.tenant_id,
        course_node_id=course_node_id,
        from_date=from_,
        to_date=to_,
        limit=limit_tasks,
        offset=offset_tasks,
    )
    return HomeworkCourseCostResponse(
        course_node_id=course_node_id,
        from_=from_,
        to_=to_,
        by_task=[
            HomeworkByTaskEntry(
                authored_document_id=row.authored_document_id,
                task_label=material_label(
                    filename=row.filename,
                    source_type=row.source_type,
                    order=row.order,
                ),
                is_deleted=row.is_deleted,
                cost_usd=row.cost_usd,
            )
            for row in by_task_rows
        ],
    )


@router.get(
    "/cost/homework/task/{authored_document_id}",
    response_model=HomeworkTaskCostResponse,
)
async def get_homework_cost_task(
    authored_document_id: uuid.UUID,
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    limit_students: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_students: Annotated[int, Query(ge=0)] = 0,
) -> HomeworkTaskCostResponse:
    """Homework cost for one task, grouped by student — the leaf (level 3).

    Each student's ``cost_usd`` sums across ALL their submissions for the task.
    A foreign or unknown task returns an empty ``by_student`` (leak-safe).
    """
    from_, to_ = await _resolve_date_range(
        session=session,
        tenant_id=tenant.tenant_id,
        from_=from_,
        to_=to_,
    )
    if from_ > to_:
        raise HTTPException(status_code=422, detail="from must be <= to")

    repo = HomeworkCostRepository(session)
    by_student_rows = await repo.get_homework_by_student(
        tenant_id=tenant.tenant_id,
        authored_document_id=authored_document_id,
        from_date=from_,
        to_date=to_,
        limit=limit_students,
        offset=offset_students,
    )
    return HomeworkTaskCostResponse(
        authored_document_id=authored_document_id,
        from_=from_,
        to_=to_,
        by_student=[
            HomeworkByStudentEntry(
                student_id=row.student_id,
                student_display=row.student_display,
                cost_usd=row.cost_usd,
            )
            for row in by_student_rows
        ],
    )


@router.get("/cost/course/{course_node_id}", response_model=CourseCostResponse)
async def get_cost_course(
    course_node_id: uuid.UUID,
    tenant: SharedDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[ArqRedis, Depends(get_arq_redis)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    limit_nodes: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_nodes: Annotated[int, Query(ge=0)] = 0,
    limit_actions: Annotated[int, Query(ge=0, le=500)] = 50,
    offset_actions: Annotated[int, Query(ge=0)] = 0,
    no_cache: Annotated[bool, Query(include_in_schema=False)] = False,
) -> CourseCostResponse:
    """Cost drill-down for a single course (subtree drill-down).

    ``from``/``to`` are optional with the same fallback semantics as
    ``/cost/summary`` — see module docstring.

    Security: ``?no_cache=true`` always hits the DB and additionally
    re-runs the recursive descendant CTE in ``get_descendant_ids``.
    Same rate-limit boundedness + audit-log story as ``/cost/summary``;
    see that endpoint's docstring.
    """
    from_, to_ = await _resolve_date_range(
        session=session,
        tenant_id=tenant.tenant_id,
        from_=from_,
        to_=to_,
    )
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

    node_repo = CourseNodeRepository(session)
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
