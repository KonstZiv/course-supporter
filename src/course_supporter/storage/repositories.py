"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.models.reports import CostReport, CostSummary, GroupedCost
from course_supporter.storage.mapping_validation import MappingValidationResult
from course_supporter.storage.orm import (
    ExternalServiceCall,
    MappingValidationState,
    SlideVideoMapping,
)


class SlideVideoMappingRepository:
    """Repository for slide-video mapping operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def batch_create(
        self,
        node_id: uuid.UUID,
        mappings: list[SlideVideoMapEntry],
        *,
        validation_results: list[MappingValidationResult] | None = None,
    ) -> list[SlideVideoMapping]:
        """Create multiple slide-video mappings in a batch.

        Args:
            node_id: FK to the parent material node.
            mappings: List of SlideVideoMapEntry Pydantic models.
            validation_results: Optional validation outcomes to persist
                on each record (validation_state, blocking_factors, etc.).

        Returns:
            List of created SlideVideoMapping ORM instances.
        """
        results_by_idx: dict[int, MappingValidationResult] = {}
        if validation_results is not None:
            results_by_idx = {r.index: r for r in validation_results}

        records = []
        for idx, m in enumerate(mappings):
            record = SlideVideoMapping(
                node_id=node_id,
                presentation_entry_id=uuid.UUID(m.presentation_entry_id),
                video_entry_id=uuid.UUID(m.video_entry_id),
                slide_number=m.slide_number,
                video_timecode_start=m.video_timecode_start,
                video_timecode_end=m.video_timecode_end,
                order=idx,
            )
            vr = results_by_idx.get(idx)
            if vr is not None:
                record.validation_state = vr.status
                record.blocking_factors = (
                    [asdict(bf) for bf in vr.blocking_factors]
                    if vr.blocking_factors
                    else None
                )
                record.validation_errors = (
                    [asdict(e) for e in vr.errors] if vr.errors else None
                )
                record.validated_at = (
                    datetime.now(UTC)
                    if vr.status == MappingValidationState.VALIDATED
                    else None
                )
            self._session.add(record)
            records.append(record)
        await self._session.flush()
        return records

    async def find_pending_by_material(
        self, material_entry_id: uuid.UUID
    ) -> list[SlideVideoMapping]:
        """Find pending_validation mappings blocked by a specific material.

        Fetches all pending mappings, filters in Python by blocking_factors
        content (project pattern: batch fetch + filter in memory).
        """
        stmt = select(SlideVideoMapping).where(
            SlideVideoMapping.validation_state
            == MappingValidationState.PENDING_VALIDATION,
            SlideVideoMapping.blocking_factors.isnot(None),
        )
        result = await self._session.execute(stmt)
        mid_str = str(material_entry_id)
        return [
            m
            for m in result.scalars().all()
            if any(
                bf.get("material_entry_id") == mid_str
                for bf in (m.blocking_factors or [])
            )
        ]

    async def get_by_id(self, mapping_id: uuid.UUID) -> SlideVideoMapping | None:
        """Get a single mapping by primary key."""
        return await self._session.get(SlideVideoMapping, mapping_id)

    async def delete(self, mapping: SlideVideoMapping) -> None:
        """Delete a mapping object directly."""
        await self._session.delete(mapping)
        await self._session.flush()

    async def get_problematic_by_node_ids(
        self, node_ids: list[uuid.UUID]
    ) -> list[SlideVideoMapping]:
        """Fetch mappings with pending_validation or validation_failed states.

        Args:
            node_ids: List of MaterialNode UUIDs to check.

        Returns:
            List of SlideVideoMapping with problematic validation states.
        """
        if not node_ids:
            return []
        stmt = (
            select(SlideVideoMapping)
            .where(
                SlideVideoMapping.node_id.in_(node_ids),
                SlideVideoMapping.validation_state.in_(
                    [
                        MappingValidationState.PENDING_VALIDATION,
                        MappingValidationState.VALIDATION_FAILED,
                    ]
                ),
            )
            .order_by(SlideVideoMapping.node_id, SlideVideoMapping.slide_number)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_node_id(self, node_id: uuid.UUID) -> list[SlideVideoMapping]:
        """Get all slide-video mappings for a material node.

        Args:
            node_id: UUID of the parent material node.

        Returns:
            List of SlideVideoMapping instances ordered by order.
        """
        stmt = (
            select(SlideVideoMapping)
            .where(SlideVideoMapping.node_id == node_id)
            .order_by(SlideVideoMapping.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class ExternalServiceCallRepository:
    """Repository for external service call analytics and cost reporting.

    Optionally scoped by tenant_id. When tenant_id is provided,
    all queries filter by it. When None, returns all records.
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
            stmt = stmt.where(ExternalServiceCall.tenant_id == self._tenant_id)
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
            stmt = stmt.where(ExternalServiceCall.tenant_id == self._tenant_id)
        result = await self._session.execute(stmt)
        return [
            GroupedCost(
                group=row.group,
                calls=row.calls,
                cost_usd=float(row.cost_usd),
                units_in=int(row.units_in),
                units_out=int(row.units_out),
                avg_latency_ms=float(row.avg_latency_ms),
            )
            for row in result.all()
        ]
