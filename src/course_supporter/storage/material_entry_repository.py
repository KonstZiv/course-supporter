"""Repository for MaterialEntry CRUD and lifecycle management."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import MaterialEntry


class MaterialEntryRepository:
    """Repository for material entry operations.

    Handles CRUD, pending receipt management, and hash invalidation.
    Not tenant-scoped — tenant isolation is ensured at the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        node_id: uuid.UUID,
        source_type: str,
        source_url: str,
        filename: str | None = None,
    ) -> MaterialEntry:
        """Create a new material entry with auto-incremented order.

        Args:
            node_id: FK to the parent MaterialNode.
            source_type: One of video, presentation, text, web.
            source_url: URL or storage path for the raw material.
            filename: Original filename (for uploads).

        Returns:
            The newly created MaterialEntry.
        """
        next_order = await self._next_sibling_order(node_id)
        entry = MaterialEntry(
            node_id=node_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            order=next_order,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def get_by_id(self, entry_id: uuid.UUID) -> MaterialEntry | None:
        """Get an entry by primary key."""
        return await self._session.get(MaterialEntry, entry_id)

    async def get_for_node(
        self, node_id: uuid.UUID, *, source_type: str | None = None
    ) -> list[MaterialEntry]:
        """Get entries for a node, ordered by position.

        Args:
            node_id: FK to the parent MaterialNode.
            source_type: Optional filter by source type.
        """
        stmt = (
            select(MaterialEntry)
            .where(MaterialEntry.node_id == node_id)
            .order_by(MaterialEntry.order)
        )
        if source_type is not None:
            stmt = stmt.where(MaterialEntry.source_type == source_type)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def set_pending(
        self,
        entry_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> MaterialEntry:
        """Mark entry as pending ingestion.

        Sets pending_job_id and pending_since, clears error_message.

        Args:
            entry_id: Entry to mark.
            job_id: FK to the Job performing ingestion.
            now: Override for current time (testing).

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        now = now or datetime.now(UTC)
        entry.pending_job_id = job_id
        entry.pending_since = now
        entry.error_message = None
        await self._session.flush()
        return entry

    async def complete_processing(
        self,
        entry_id: uuid.UUID,
        *,
        processed_content: str,
        processed_hash: str,
        now: datetime | None = None,
    ) -> MaterialEntry:
        """Mark entry as successfully processed.

        Clears pending receipt and sets processed layer.

        Args:
            entry_id: Entry to update.
            processed_content: Extracted/processed text content.
            processed_hash: SHA-256 hash of raw source at processing time.
            now: Override for current time (testing).

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        now = now or datetime.now(UTC)
        entry.processed_content = processed_content
        entry.processed_hash = processed_hash
        entry.processed_at = now
        entry.content_fingerprint = None  # invalidate — content changed
        entry.pending_job_id = None
        entry.pending_since = None
        entry.error_message = None
        await self._session.flush()
        return entry

    async def fail_processing(
        self,
        entry_id: uuid.UUID,
        *,
        error_message: str,
    ) -> MaterialEntry:
        """Mark entry as failed processing.

        Clears pending receipt and sets error_message.

        Args:
            entry_id: Entry to update.
            error_message: Human-readable error description.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.pending_job_id = None
        entry.pending_since = None
        entry.error_message = error_message
        await self._session.flush()
        return entry

    async def update_source(
        self,
        entry_id: uuid.UUID,
        *,
        source_url: str,
        filename: str | None = None,
    ) -> MaterialEntry:
        """Update source URL and invalidate raw hash.

        When the source changes, raw_hash is cleared to signal that
        the processed layer is potentially stale. This triggers
        INTEGRITY_BROKEN state if processed_content exists.

        Args:
            entry_id: Entry to update.
            source_url: New source URL.
            filename: New filename (or None to clear).

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.source_url = source_url
        entry.filename = filename
        entry.raw_hash = None
        entry.raw_size_bytes = None
        entry.content_fingerprint = None  # invalidate — source changed
        await self._session.flush()
        return entry

    async def ensure_raw_hash(
        self,
        entry_id: uuid.UUID,
        *,
        raw_bytes: bytes,
    ) -> MaterialEntry:
        """Lazily compute and set raw_hash from content bytes.

        Only sets the hash if it is currently None.

        Args:
            entry_id: Entry to update.
            raw_bytes: Raw content bytes for hashing.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        if entry.raw_hash is None:
            entry.raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            entry.raw_size_bytes = len(raw_bytes)
            await self._session.flush()
        return entry

    async def delete(self, entry_id: uuid.UUID) -> None:
        """Delete an entry.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        await self._session.delete(entry)
        await self._session.flush()

    # ── Private helpers ──

    async def _require(self, entry_id: uuid.UUID) -> MaterialEntry:
        """Get entry or raise ValueError."""
        entry = await self.get_by_id(entry_id)
        if entry is None:
            msg = f"MaterialEntry not found: {entry_id}"
            raise ValueError(msg)
        return entry

    async def _next_sibling_order(self, node_id: uuid.UUID) -> int:
        """Get next order value for entries under the given node."""
        stmt = select(func.coalesce(func.max(MaterialEntry.order) + 1, 0)).where(
            MaterialEntry.node_id == node_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
