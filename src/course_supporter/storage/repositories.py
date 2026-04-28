"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import SoftDeleteMixin


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
    """Repository for ExternalServiceCall (KD5).

    Empty placeholder after the 0.4 ESC redesign — legacy aggregate
    methods (``get_summary``, ``get_by_*``, ``get_full_report``) and
    their tenant-filter parameter were removed together with the
    ``ESC.tenant_id`` column. Cost-summary query methods land in
    commit (e); writes flow through ``service_logging._persist``
    (single ESC write surface).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
