"""Repository for MaterialSegment CRUD.

PR #4c only needs batch-insert plus a simple read for tests and
downstream verification. Segments are hard-cascaded when the parent
macro section is deleted via the ORM CASCADE constraint.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from course_supporter.storage.orm import MaterialSegment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.models.macro_segment import SegmentDraft


class MaterialSegmentRepository:
    """CRUD operations for ``material_segments``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def batch_create(
        self,
        *,
        macro_section_id: uuid.UUID,
        drafts: list[SegmentDraft],
        llm_call_id: uuid.UUID | None,
    ) -> list[MaterialSegment]:
        """Insert one row per draft. Flushes but does not commit."""
        records: list[MaterialSegment] = []
        for draft in drafts:
            record = MaterialSegment(
                macro_section_id=macro_section_id,
                order=draft.order,
                start_pos=draft.start_pos,
                end_pos=draft.end_pos,
                content=draft.content,
                llm_call_id=llm_call_id,
            )
            self._session.add(record)
            records.append(record)
        await self._session.flush()
        return records

    async def list_by_macro(self, macro_section_id: uuid.UUID) -> list[MaterialSegment]:
        """Return all segments of a macro section, ordered by ``order``."""
        stmt = (
            select(MaterialSegment)
            .where(MaterialSegment.macro_section_id == macro_section_id)
            .order_by(MaterialSegment.order.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
