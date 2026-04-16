"""Repository for MaterialMacroSection CRUD.

Scoped to the operations PR #4c needs: batch-create a newly produced
set of sections, and read them back for a given material entry.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from course_supporter.storage.orm import (
    MacroSectionStatus,
    MaterialMacroSection,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.models.macro_segment import MacroSectionDraft


class MaterialMacroSectionRepository:
    """CRUD operations for ``material_macro_sections``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def batch_create(
        self,
        *,
        material_entry_id: uuid.UUID,
        drafts: list[MacroSectionDraft],
        llm_call_id: uuid.UUID | None,
        status: MacroSectionStatus = MacroSectionStatus.READY,
    ) -> list[MaterialMacroSection]:
        """Insert one row per draft and return the ORM instances.

        Rows are flushed so callers can use the generated ``id``
        immediately (e.g. to attach segments) but the session is
        **not** committed — the caller owns the transaction.
        """
        records: list[MaterialMacroSection] = []
        for draft in drafts:
            record = MaterialMacroSection(
                material_entry_id=material_entry_id,
                order=draft.order,
                title=draft.title,
                start_pos=draft.start_pos,
                end_pos=draft.end_pos,
                status=status,
                llm_call_id=llm_call_id,
            )
            self._session.add(record)
            records.append(record)
        await self._session.flush()
        return records

    async def list_by_entry(
        self, material_entry_id: uuid.UUID
    ) -> list[MaterialMacroSection]:
        """Return all macro sections for an entry, ordered by ``order``."""
        stmt = (
            select(MaterialMacroSection)
            .where(MaterialMacroSection.material_entry_id == material_entry_id)
            .order_by(MaterialMacroSection.order.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete_by_entry(self, material_entry_id: uuid.UUID) -> int:
        """Hard-delete all macro sections for an entry (cascades to segments).

        Returns the number of rows removed. Used by the retry / reprocess
        flow once it lands — kept out of the 4c critical path.
        """
        existing = await self.list_by_entry(material_entry_id)
        for row in existing:
            await self._session.delete(row)
        await self._session.flush()
        return len(existing)
