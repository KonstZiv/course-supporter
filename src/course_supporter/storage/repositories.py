"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from course_supporter.models.reports import CostReport, CostSummary, GroupedCost
from course_supporter.storage.orm import ExternalServiceCall, SoftDeleteMixin


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


class ExternalServiceCallRepository:
    """Repository for external service call analytics and cost reporting.

    Scoped by tenant_id: includes records with matching tenant_id
    AND records with tenant_id=NULL (legacy/pre-tracking records).
    When tenant_id is None, returns all records.
    """

    def __init__(
        self, session: AsyncSession, tenant_id: uuid.UUID | None = None
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get_summary(self) -> CostSummary:
        """Get aggregate summary of LLM calls."""
        stmt = select(
            func.count().label("total_calls"),
            func.count()
            .filter(ExternalServiceCall.success.is_(True))
            .label("successful_calls"),
            func.count()
            .filter(ExternalServiceCall.success.is_(False))
            .label("failed_calls"),
            func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                "total_cost_usd"
            ),
            func.coalesce(func.sum(ExternalServiceCall.unit_in), 0).label(
                "total_units_in"
            ),
            func.coalesce(func.sum(ExternalServiceCall.unit_out), 0).label(
                "total_units_out"
            ),
            func.coalesce(func.avg(ExternalServiceCall.latency_ms), 0.0).label(
                "avg_latency_ms"
            ),
        ).select_from(ExternalServiceCall)
        if self._tenant_id is not None:
            stmt = stmt.where(
                or_(
                    ExternalServiceCall.tenant_id == self._tenant_id,
                    ExternalServiceCall.tenant_id.is_(None),
                )
            )
        result = await self._session.execute(stmt)
        row = result.one()
        return CostSummary(
            total_calls=row.total_calls,
            successful_calls=row.successful_calls,
            failed_calls=row.failed_calls,
            total_cost_usd=float(row.total_cost_usd),
            total_units_in=int(row.total_units_in),
            total_units_out=int(row.total_units_out),
            avg_latency_ms=float(row.avg_latency_ms),
        )

    async def get_full_report(self) -> CostReport:
        """Get complete cost report with summary and all breakdowns."""
        return CostReport(
            summary=await self.get_summary(),
            by_action=await self.get_by_action(),
            by_provider=await self.get_by_provider(),
            by_model=await self.get_by_model(),
        )

    async def get_by_action(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by action."""
        return await self._grouped_query(ExternalServiceCall.action)

    async def get_by_provider(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by provider."""
        return await self._grouped_query(ExternalServiceCall.provider)

    async def get_by_model(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by model_id."""
        return await self._grouped_query(ExternalServiceCall.model_id)

    async def _grouped_query(
        self,
        group_column: InstrumentedAttribute[str],
    ) -> list[GroupedCost]:
        """Run a GROUP BY query on the given column."""
        stmt = (
            select(
                group_column.label("group"),
                func.count().label("calls"),
                func.count()
                .filter(ExternalServiceCall.success.is_(True))
                .label("successful_calls"),
                func.count()
                .filter(ExternalServiceCall.success.is_(False))
                .label("failed_calls"),
                func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0).label(
                    "cost_usd"
                ),
                func.coalesce(func.sum(ExternalServiceCall.unit_in), 0).label(
                    "units_in"
                ),
                func.coalesce(func.sum(ExternalServiceCall.unit_out), 0).label(
                    "units_out"
                ),
                func.coalesce(func.avg(ExternalServiceCall.latency_ms), 0.0).label(
                    "avg_latency_ms"
                ),
            )
            .select_from(ExternalServiceCall)
            .group_by(group_column)
            .order_by(func.count().desc())
        )
        if self._tenant_id is not None:
            stmt = stmt.where(
                or_(
                    ExternalServiceCall.tenant_id == self._tenant_id,
                    ExternalServiceCall.tenant_id.is_(None),
                )
            )
        result = await self._session.execute(stmt)
        return [
            GroupedCost(
                group=row.group,
                calls=row.calls,
                successful_calls=row.successful_calls,
                failed_calls=row.failed_calls,
                cost_usd=float(row.cost_usd),
                units_in=int(row.units_in),
                units_out=int(row.units_out),
                avg_latency_ms=float(row.avg_latency_ms),
            )
            for row in result.all()
        ]
