"""Repository for ReconciliationPreview CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import ReconciliationPreview


class ReconciliationPreviewRepository:
    """Repository for reconciliation preview cache operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_fingerprint(
        self,
        materialnode_id: uuid.UUID,
        combined_fingerprint: str,
    ) -> ReconciliationPreview | None:
        """Find a preview by unique (materialnode_id, combined_fingerprint)."""
        stmt = select(ReconciliationPreview).where(
            ReconciliationPreview.materialnode_id == materialnode_id,
            ReconciliationPreview.combined_fingerprint == combined_fingerprint,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        materialnode_id: uuid.UUID,
        combined_fingerprint: str,
        node_fingerprint: str,
        editable_tree_hash: str,
        issues: list[dict[str, Any]],
        context_summary: str | None,
        job_id: uuid.UUID | None,
    ) -> ReconciliationPreview:
        """Insert or update a reconciliation preview by fingerprint.

        If a preview with the same (materialnode_id, combined_fingerprint)
        exists, updates its issues and context_summary. Otherwise creates
        a new record.
        """
        existing = await self.find_by_fingerprint(materialnode_id, combined_fingerprint)
        if existing is not None:
            existing.issues = issues
            existing.context_summary = context_summary
            existing.job_id = job_id
            await self._session.flush()
            return existing

        preview = ReconciliationPreview(
            materialnode_id=materialnode_id,
            combined_fingerprint=combined_fingerprint,
            node_fingerprint=node_fingerprint,
            editable_tree_hash=editable_tree_hash,
            issues=issues,
            context_summary=context_summary,
            job_id=job_id,
        )
        self._session.add(preview)
        await self._session.flush()
        return preview

    async def get_latest(
        self,
        materialnode_id: uuid.UUID,
    ) -> ReconciliationPreview | None:
        """Get the most recent preview for a MaterialNode."""
        stmt = (
            select(ReconciliationPreview)
            .where(ReconciliationPreview.materialnode_id == materialnode_id)
            .order_by(ReconciliationPreview.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
