"""Merkle fingerprint service for material tree integrity tracking."""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import MaterialEntry


class FingerprintService:
    """Lazy-cached fingerprint calculations for the material tree.

    Fingerprints are computed on demand and stored in the ORM objects.
    A ``None`` fingerprint means "stale / never computed". Calling
    ``ensure_*`` either returns the cached value or computes, persists
    (flush), and returns the new one.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_material_fp(self, entry: MaterialEntry) -> str:
        """Lazily compute and cache content_fingerprint.

        Returns the existing fingerprint if already set. Otherwise
        computes ``sha256(processed_content)`` in UTF-8, stores it on
        the entry, flushes, and returns the hex digest.

        Args:
            entry: A loaded MaterialEntry ORM instance.

        Returns:
            64-char lowercase hex SHA-256 digest.

        Raises:
            ValueError: If ``processed_content`` is None (entry not
                yet processed).
        """
        if entry.content_fingerprint is not None:
            return entry.content_fingerprint

        if entry.processed_content is None:
            msg = (
                f"Cannot compute fingerprint: MaterialEntry {entry.id} "
                f"has no processed_content"
            )
            raise ValueError(msg)

        fp = hashlib.sha256(entry.processed_content.encode()).hexdigest()
        entry.content_fingerprint = fp
        await self._session.flush()
        return fp
