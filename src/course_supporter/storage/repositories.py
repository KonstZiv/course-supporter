"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import (
    CourseNode,
    ExternalServiceCall,
    Job,
    SoftDeleteMixin,
)


class SoftDeleteRepository[ModelT: SoftDeleteMixin]:
    """Base repository for entities with the soft-delete pattern.

    Provides three filtered listing flavors and an idempotent
    ``soft_delete`` setter (vision §3 KD3, KD12). Subclasses bind a
    concrete model by setting the ``model`` class attribute:

        class TenantRepository(SoftDeleteRepository[Tenant]):
            model = Tenant

    Methods do not commit; the caller controls the transaction
    boundary, matching the project's existing repository style.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active(self) -> list[ModelT]:
        """List rows where ``deleted_at IS NULL``."""
        stmt = select(self.model).where(self.model.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_including_deleted(self) -> list[ModelT]:
        """List all rows regardless of soft-delete state.

        Use for audit, cost attribution, and other read paths that
        must see historical entities.
        """
        stmt = select(self.model)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_only_deleted(self) -> list[ModelT]:
        """List rows where ``deleted_at IS NOT NULL``.

        Use for orphan cleanup tasks (e.g., S3 hard-delete sweeper).
        """
        stmt = select(self.model).where(self.model.deleted_at.is_not(None))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete(
        self,
        entity_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ModelT | None:
        """Set ``deleted_at`` on the row identified by ``entity_id``.

        Returns the entity (or ``None`` if not found). Idempotent —
        re-calling on an already-deleted row is a no-op and does not
        trigger an UPDATE (which would be blocked by the DB-level
        soft-delete protection trigger).
        """
        entity = await self._session.get(self.model, entity_id)
        if entity is None:
            return None
        if entity.deleted_at is not None:
            return entity
        entity.deleted_at = now if now is not None else datetime.now(UTC)
        await self._session.flush()
        return entity


class ByCourseRow(NamedTuple):
    """One course row in /cost/summary by_course breakdown."""

    course_node_id: uuid.UUID
    course_title: str
    cost_usd: float


class ByProviderRow(NamedTuple):
    """One (provider, model_id) row in /cost/summary by_provider breakdown."""

    provider: str
    model_id: str
    cost_usd: float


class ByNodeRow(NamedTuple):
    """One node row in /cost/course/{id} by_node breakdown."""

    course_node_id: uuid.UUID
    title: str
    cost_usd: float


class ByActionRow(NamedTuple):
    """One action row in /cost/course/{id} by_action breakdown."""

    action: str
    cost_usd: float


def _to_exclusive(to_date: date) -> date:
    """Convert inclusive end-date to exclusive (``< to + 1 day``).

    The endpoint contract is inclusive on both bounds (``from..to`` covers
    all of ``to``-day). Implementing as ``created_at >= :from AND
    created_at < :to_exclusive`` cleanly handles the time component of
    ``timestamptz`` without timezone gymnastics.
    """
    return to_date + timedelta(days=1)


class ExternalServiceCallRepository:
    """Repository for ExternalServiceCall queries (KD5).

    Cost-reporting query methods that materialise into the Pydantic
    response models declared in :mod:`course_supporter.models.cost`.
    All methods are read-only aggregations; ESC writes flow through
    :func:`course_supporter.service_logging._persist` (single write
    surface) plus three direct in-transaction writers in
    ``api/tasks.py`` that need ``esc.id`` for FK back-pointers.

    Common SQL invariants across the breakdown methods:

    * Tenant scope via ``JOIN jobs ON ESC.job_id = jobs.id WHERE
      jobs.tenant_id = :tenant_id`` — KD5 dropped ``ESC.tenant_id``,
      attribution flows through Job.
    * Date range ``ESC.created_at >= :from AND ESC.created_at <
      :to_exclusive`` where ``:to_exclusive = :to + 1 day`` (inclusive
      end-of-day).
    * ``WHERE cost_usd IS NOT NULL`` — NULL means *unknown cost* (failed
      LLM call before billing computed), not zero. Excluded from sums.
    * ``ORDER BY SUM(cost_usd) DESC`` on every breakdown — cost-priority
      first, predictable for UI.
    * Pagination (``LIMIT/OFFSET``) only on aggregated breakdowns;
      scalar totals are not paginated.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_total_for_period(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
    ) -> float:
        """Sum of attributed and unattributed ESC costs in the period.

        Includes Jobs with ``course_node_id IS NULL`` (orphan jobs).
        Returns ``0.0`` if no rows match.
        """
        stmt = (
            select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0))
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
        )
        result = await self._session.execute(stmt)
        # COALESCE guarantees non-NULL; ``or 0.0`` placates SQLA stubs.
        return float(result.scalar_one() or 0.0)

    async def get_total_for_subtree(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
    ) -> float:
        """Sum of ESC costs for Jobs whose ``course_node_id`` is in subtree.

        Per-course total for ``/cost/course/{id}`` — distinct from
        :meth:`get_total_for_period` (tenant-wide). Empty
        ``course_node_ids`` short-circuits to ``0.0`` (no SQL emitted).

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return 0.0
        stmt = (
            select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0))
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
        )
        result = await self._session.execute(stmt)
        # COALESCE guarantees non-NULL; ``or 0.0`` placates SQLA stubs.
        return float(result.scalar_one() or 0.0)

    async def get_unattributed_for_period(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
    ) -> float:
        """Sum of ESC costs for Jobs with ``course_node_id IS NULL``.

        Invariant on the route layer:
        ``total == sum(by_course.cost_usd) + unattributed``.
        Returns ``0.0`` if no rows match.
        """
        stmt = (
            select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0))
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.is_(None),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
        )
        result = await self._session.execute(stmt)
        # COALESCE guarantees non-NULL; ``or 0.0`` placates SQLA stubs.
        return float(result.scalar_one() or 0.0)

    async def get_by_course(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByCourseRow]:
        """Cost grouped by ``Job.course_node_id`` (Variant D — direct).

        Groups directly by whichever node the Job points at; if Jobs
        target sub-course nodes (e.g. lessons), they appear as their
        own rows. Course-root aggregation is the responsibility of the
        UI client. See POST-MR-NOTES forward-looking note for the
        future Phase 1+ walk-up CTE option.

        Excludes orphan Jobs (``course_node_id IS NULL``) — those go
        into ``unattributed_cost_usd`` via
        :meth:`get_unattributed_for_period`.
        """
        stmt = (
            select(
                CourseNode.id.label("course_node_id"),
                CourseNode.title.label("course_title"),
                func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                    "cost_usd"
                ),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
            .group_by(CourseNode.id, CourseNode.title)
            .order_by(func.sum(ExternalServiceCall.cost_usd).desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByCourseRow(
                course_node_id=row.course_node_id,
                course_title=row.course_title,
                cost_usd=float(row.cost_usd),
            )
            for row in result.all()
        ]

    async def get_by_provider(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByProviderRow]:
        """Cost grouped by ``(ESC.provider, ESC.model_id)``.

        Includes Jobs with ``course_node_id IS NULL`` — provider-level
        breakdown is independent of course attribution.
        """
        stmt = (
            select(
                ExternalServiceCall.provider.label("provider"),
                ExternalServiceCall.model_id.label("model_id"),
                func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                    "cost_usd"
                ),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
            .group_by(ExternalServiceCall.provider, ExternalServiceCall.model_id)
            .order_by(func.sum(ExternalServiceCall.cost_usd).desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByProviderRow(
                provider=row.provider,
                model_id=row.model_id,
                cost_usd=float(row.cost_usd),
            )
            for row in result.all()
        ]

    async def get_by_node(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByNodeRow]:
        """Cost grouped by node within a pre-resolved subtree.

        ``course_node_ids`` is the descendant set of the requested course
        root, resolved on the route layer via
        :meth:`CourseNodeRepository.get_descendant_ids`. Empty list →
        empty result (no SQL).

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return []
        stmt = (
            select(
                CourseNode.id.label("course_node_id"),
                CourseNode.title.label("title"),
                func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                    "cost_usd"
                ),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
            .group_by(CourseNode.id, CourseNode.title)
            .order_by(func.sum(ExternalServiceCall.cost_usd).desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByNodeRow(
                course_node_id=row.course_node_id,
                title=row.title,
                cost_usd=float(row.cost_usd),
            )
            for row in result.all()
        ]

    async def get_by_action(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByActionRow]:
        """Cost grouped by ``ESC.action`` within a pre-resolved subtree.

        Same subtree contract as :meth:`get_by_node`. Empty list →
        empty result.

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return []
        stmt = (
            select(
                ExternalServiceCall.action.label("action"),
                func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                    "cost_usd"
                ),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                ExternalServiceCall.cost_usd.is_not(None),
            )
            .group_by(ExternalServiceCall.action)
            .order_by(func.sum(ExternalServiceCall.cost_usd).desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByActionRow(action=row.action, cost_usd=float(row.cost_usd))
            for row in result.all()
        ]
