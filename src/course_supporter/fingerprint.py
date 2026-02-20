"""Merkle fingerprint service for material tree integrity tracking."""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import MaterialEntry, MaterialNode


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

    async def ensure_node_fp(self, node: MaterialNode) -> str:
        """Lazily compute and cache Merkle fingerprint for a node.

        Combines material fingerprints (``m:<hex>``) and child node
        fingerprints (``n:<hex>``) into a sorted list, joins with
        newline, and hashes with SHA-256. Recurses into children
        first (bottom-up).

        Materials without ``processed_content`` are skipped — the
        fingerprint reflects only processed materials.

        The node's ``materials`` and ``children`` relationships must
        be loaded before calling this method.

        Args:
            node: A loaded MaterialNode ORM instance with eagerly
                loaded ``materials`` and ``children``.

        Returns:
            64-char lowercase hex SHA-256 digest.
        """
        if node.node_fingerprint is not None:
            return node.node_fingerprint

        parts: list[str] = []

        # Material fingerprints (skip unprocessed)
        for mat in node.materials:
            if mat.processed_content is not None:
                fp = await self.ensure_material_fp(mat)
                parts.append(f"m:{fp}")

        # Child node fingerprints (recursive)
        for child in node.children:
            fp = await self.ensure_node_fp(child)
            parts.append(f"n:{fp}")

        parts.sort()
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        node.node_fingerprint = digest
        await self._session.flush()
        return digest
