"""Cascade soft-delete service (vision §3 KD3, KD12).

Engine for recursive soft-delete across declared FK relationships.
This module ships only the engine; concrete ``cascade_map`` definitions
land per-entity in subsequent phase tasks. The mapping is consumed
either as an explicit dict or built dynamically from each model's
``__cascades_soft_delete_to__`` class attribute.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper


class SoftDeletableEntity(Protocol):
    """Structural type for any model that participates in cascade delete.

    Concrete soft-deletable models (mixed with ``SoftDeleteMixin``) all
    expose these two attributes — ``id`` is declared on each model and
    ``deleted_at`` is contributed by the mixin.
    """

    id: uuid.UUID
    deleted_at: datetime | None


OnCancelJobs = Callable[[list[uuid.UUID]], Awaitable[None]]
"""Hook invoked once per cascade with all entity ids about to be deleted.

Concrete Job-cancellation logic lives outside this engine — wired by
the caller in task 0.3 and in per-entity tasks in later phases.
"""


def build_cascade_map(root: type) -> dict[type, list[type]]:
    """Build a cascade_map from ``__cascades_soft_delete_to__`` declarations.

    Walks the transitive closure starting from ``root``: every visited
    type contributes its ``__cascades_soft_delete_to__`` list as the
    map entry, and each direct descendant is then recursed into.

    Returns ``{type: [direct descendant types]}`` suitable for
    :meth:`CascadeDeleteService.soft_delete_with_cascade`.
    """
    cascade_map: dict[type, list[type]] = {}
    pending: list[type] = [root]
    while pending:
        cls = pending.pop()
        if cls in cascade_map:
            continue
        descendants: list[type] = list(getattr(cls, "__cascades_soft_delete_to__", []))
        cascade_map[cls] = descendants
        pending.extend(descendants)
    return cascade_map


class CascadeDeleteService:
    """Recursive soft-delete engine (vision §3 KD3, KD12).

    Walks the ``cascade_map`` from the root entity, collecting every
    reachable active descendant via foreign-key inspection, then writes
    ``deleted_at`` on all of them in the same DB session. The caller
    controls the transaction boundary — ``commit`` is not invoked here,
    so a failure mid-traversal rolls back the whole cascade with the
    surrounding transaction.

    The skeleton ships with no concrete cascade maps wired up; those
    arrive per-entity in subsequent phase tasks. ``build_cascade_map``
    constructs the map at runtime from each model's
    ``__cascades_soft_delete_to__`` declaration.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def soft_delete_with_cascade(
        self,
        entity: SoftDeletableEntity,
        cascade_map: dict[type, list[type]],
        *,
        on_cancel_jobs: OnCancelJobs | None = None,
        now: datetime | None = None,
    ) -> None:
        """Soft-delete ``entity`` and every active descendant.

        Idempotent: returns immediately if ``entity`` is already
        soft-deleted (no UPDATE, no hook call). Cycle-safe via a
        ``visited`` set keyed by ``(type, id)`` so cascade_map cycles
        cannot trigger infinite recursion.

        ``on_cancel_jobs`` is invoked once before any UPDATE with the
        full list of collected ids. ``now`` overrides the timestamp
        applied to all rows (defaults to ``datetime.now(UTC)``);
        useful for deterministic tests.
        """
        if entity.deleted_at is not None:
            return

        ts = now if now is not None else datetime.now(UTC)
        visited: set[tuple[type, uuid.UUID]] = set()
        to_delete: list[SoftDeletableEntity] = []

        await self._collect(entity, cascade_map, visited, to_delete)

        if on_cancel_jobs is not None:
            await on_cancel_jobs([e.id for e in to_delete])

        for row in to_delete:
            row.deleted_at = ts

        await self._session.flush()

    async def _collect(
        self,
        entity: SoftDeletableEntity,
        cascade_map: dict[type, list[type]],
        visited: set[tuple[type, uuid.UUID]],
        to_delete: list[SoftDeletableEntity],
    ) -> None:
        cls = type(entity)
        key = (cls, entity.id)
        if key in visited:
            return
        visited.add(key)
        if entity.deleted_at is not None:
            return

        to_delete.append(entity)

        for child_cls in cascade_map.get(cls, []):
            for child in await self._fetch_active_children(entity, child_cls):
                await self._collect(child, cascade_map, visited, to_delete)

    async def _fetch_active_children(
        self,
        parent: SoftDeletableEntity,
        child_cls: type,
    ) -> list[SoftDeletableEntity]:
        """Return active rows of ``child_cls`` linked to ``parent`` by FK.

        Resolves the relationship by inspecting ``child_cls``'s mapped
        columns and picking the one that references the parent's
        mapped table. Raises ``ValueError`` on zero or multiple matches
        — ambiguous cascade relationships must be made explicit by the
        caller (e.g., split the descendant into a separate type, or
        omit it from the cascade map).
        """
        parent_mapper = cast(Mapper[Any], inspect(type(parent)))
        child_mapper = cast(Mapper[Any], inspect(child_cls))
        parent_table = parent_mapper.local_table

        fk_cols = [
            col
            for col in child_mapper.local_table.columns
            for fk in col.foreign_keys
            if fk.column.table is parent_table
        ]
        if not fk_cols:
            raise ValueError(
                f"No foreign key from {child_cls.__name__} to {type(parent).__name__}"
            )
        if len(fk_cols) > 1:
            raise ValueError(
                f"Ambiguous foreign keys from {child_cls.__name__} "
                f"to {type(parent).__name__}: {[c.name for c in fk_cols]}"
            )

        fk_col = fk_cols[0]
        deleted_at_col: Any = child_cls.deleted_at  # type: ignore[attr-defined]
        stmt: Any = (
            select(child_cls).where(fk_col == parent.id).where(deleted_at_col.is_(None))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
