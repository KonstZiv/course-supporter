"""Repository for MaterialEntry CRUD and lifecycle management."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.models.methodist import AssignmentType
from course_supporter.models.source import MaterialRole
from course_supporter.storage.orm import MaterialEntry, MaterialNode


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
        material_role: str = "educational",
        task_type: AssignmentType | str | None = None,
        language: str | None = None,
    ) -> MaterialEntry:
        """Create a new material entry with auto-incremented order.

        Args:
            node_id: FK to the parent MaterialNode.
            source_type: One of video, presentation, text, web.
            source_url: URL or storage path for the raw material.
            filename: Original filename (for uploads).
            material_role: Role: educational or methodological.
            task_type: Optional AssignmentType when this material represents
                a concrete task (test, short_task, task, project). NULL for
                regular materials.
            language: Optional ISO 639-1 language override. When None,
                the course default is used and STT falls back to
                auto-detection (which caches its result back here).

        Returns:
            The newly created MaterialEntry.
        """
        next_order = await self._next_sibling_order(node_id)
        task_type_value: str | None
        if isinstance(task_type, AssignmentType):
            task_type_value = task_type.value
        else:
            task_type_value = task_type
        entry = MaterialEntry(
            materialnode_id=node_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            material_role=material_role,
            task_type=task_type_value,
            language=language,
            order=next_order,
        )
        self._session.add(entry)
        await self._session.flush()
        await self._invalidate_node_chain(node_id)
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
            .where(MaterialEntry.materialnode_id == node_id)
            .order_by(MaterialEntry.order)
        )
        if source_type is not None:
            stmt = stmt.where(MaterialEntry.source_type == source_type)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_source_url(self, source_url: str) -> MaterialEntry | None:
        """Find an entry by its source_url (exact match)."""
        stmt = select(MaterialEntry).where(MaterialEntry.source_url == source_url)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_by_raw_hash(self, raw_hash: str) -> MaterialEntry | None:
        """Find the oldest active MaterialEntry with the given ``raw_hash``.

        Used by ingestion to detect re-uploads of identical content
        (vision §3 KD9 + KD4). Filters out soft-deleted rows
        (``deleted_at IS NULL``); v0.20 intentionally allows the same
        ``raw_hash`` to appear on multiple active rows, so ``.first()``
        is the right shape rather than ``.scalar_one_or_none()``.
        Ordered by ``id`` (UUIDv7, time-ordered) so the result is
        deterministic across calls — duplicate-hit logging and tests
        do not flap when the index iteration order changes. ``id``
        carries a unique index, so the ORDER BY adds no meaningful
        cost. Callers needing tenant scoping or "all matches" should
        issue a custom query.
        """
        stmt = (
            select(MaterialEntry)
            .where(MaterialEntry.raw_hash == raw_hash)
            .where(MaterialEntry.deleted_at.is_(None))
            .order_by(MaterialEntry.id)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def set_language_if_unset(
        self,
        entry_id: uuid.UUID,
        language: str,
    ) -> bool:
        """Atomically set ``language`` only if it is currently NULL.

        Guards against races where a concurrent PATCH may set the
        language between our read and write. Returns True if the row
        was updated, False if it was skipped because the language
        was already set (or the row does not exist).
        """
        stmt = (
            update(MaterialEntry)
            .where(
                MaterialEntry.id == entry_id,
                MaterialEntry.language.is_(None),
            )
            .values(language=language)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        # ``rowcount`` is provided by CursorResult (actual runtime type
        # for DML execute); SQLAlchemy's static return type is the wider
        # ``Result`` which does not expose it — hence the ignore.
        return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def set_pending(
        self,
        entry_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> MaterialEntry:
        """Mark entry as pending ingestion.

        Sets job_id and pending_since, clears error_message.

        Args:
            entry_id: Entry to mark.
            job_id: FK to the Job performing ingestion.
            now: Override for current time (testing).

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        now = now or datetime.now(UTC)
        entry.job_id = job_id
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
        entry.job_id = None
        entry.pending_since = None
        entry.error_message = None
        await self._session.flush()
        await self._invalidate_node_chain(entry.materialnode_id)
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
        entry.job_id = None
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
        await self._session.flush()
        await self._invalidate_node_chain(entry.materialnode_id)
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

    async def save_outline(
        self,
        entry_id: uuid.UUID,
        *,
        outline_json: str,
    ) -> MaterialEntry:
        """Save MaterialOutline JSON for a processed entry.

        Stores the lossless restructured outline alongside the raw
        processed_content. Does not invalidate fingerprints — the
        outline is a derivative, not a source of identity.

        Args:
            entry_id: Entry to update.
            outline_json: Serialized MaterialOutline JSON.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.outline_content = outline_json
        await self._session.flush()
        return entry

    async def update_material_role(
        self,
        entry: MaterialEntry,
        *,
        material_role: MaterialRole,
    ) -> MaterialEntry:
        """Update the material_role field on an already-loaded entry.

        Args:
            entry: Entry ORM model to update.
            material_role: New role (MaterialRole.EDUCATIONAL or METHODOLOGICAL).
        """
        entry.material_role = material_role.value
        await self._session.flush()
        return entry

    async def update_task_type(
        self,
        entry: MaterialEntry,
        *,
        task_type: AssignmentType | None,
    ) -> MaterialEntry:
        """Update the task_type field on an already-loaded entry.

        Args:
            entry: Entry ORM model to update.
            task_type: New AssignmentType, or None to clear the task flag.
        """
        entry.task_type = task_type.value if task_type is not None else None
        await self._session.flush()
        return entry

    async def delete(self, entry_id: uuid.UUID) -> None:
        """Delete an entry and invalidate parent node fingerprints.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        node_id = entry.materialnode_id
        await self._session.delete(entry)
        await self._session.flush()
        await self._invalidate_node_chain(node_id)

    # ── Private helpers ──

    async def _invalidate_node_chain(self, node_id: uuid.UUID) -> None:
        """Invalidate fingerprints from node up to root."""
        from course_supporter.fingerprint import FingerprintService

        node = await self._session.get(MaterialNode, node_id)
        if node is not None:
            await FingerprintService(self._session).invalidate_up(node)

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
            MaterialEntry.materialnode_id == node_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
