"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from course_supporter.models.reports import CostReport, CostSummary, GroupedCost
from course_supporter.storage.orm import ExternalServiceCall


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
