"""Merkle fingerprint service for material tree integrity tracking."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import AuthoredDocument, CourseNode

if TYPE_CHECKING:
    from course_supporter.storage.orm import StructureNodeEditable


def compute_editable_tree_hash(flat: list[StructureNodeEditable]) -> str:
    """Compute a stable SHA-256 hash of the editable tree content fields.

    For each editable node, serialises content fields (from
    ``_CONTENT_FIELDS``) to canonical JSON (``sort_keys=True``).
    Nodes are sorted by ``id`` for deterministic ordering.

    Args:
        flat: Flat list of StructureNodeEditable ORM objects.

    Returns:
        64-char lowercase hex SHA-256 digest.
    """
    from course_supporter.storage.editable_conversion import _CONTENT_FIELDS

    parts: list[str] = []
    for node in sorted(flat, key=lambda n: str(n.id)):
        content: dict[str, object] = {}
        for field in _CONTENT_FIELDS:
            content[field] = getattr(node, field)
        parts.append(json.dumps(content, sort_keys=True, default=str))

    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class FingerprintService:
    """Lazy-cached fingerprint calculations for the material tree.

    Fingerprints are computed on demand and stored in the ORM objects.
    A ``None`` fingerprint means "stale / never computed". Calling
    ``ensure_*`` either returns the cached value or computes, persists
    (flush), and returns the new one.

    Compute methods (``_compute_*``) set attributes on ORM objects
    without flushing. Public ``ensure_*`` methods call compute, then
    flush once — avoiding N flushes in recursive tree walks.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Public API ──

    async def ensure_material_fp(self, entry: AuthoredDocument) -> str:
        """Return the material fingerprint (``processed_hash``).

        Args:
            entry: A loaded AuthoredDocument ORM instance.

        Returns:
            64-char lowercase hex SHA-256 digest.

        Raises:
            ValueError: If ``processed_hash`` is None (entry not
                yet processed).
        """
        return self._compute_material_fp(entry)

    async def ensure_node_fp(self, node: CourseNode) -> str:
        """Lazily compute and cache Merkle fingerprint for a node.

        Combines material fingerprints (``m:<hex>``) and child node
        fingerprints (``n:<hex>``) into a sorted list, joins with
        newline, and hashes with SHA-256. Recurses into children
        first (bottom-up).

        Materials without ``processed_content`` are skipped — the
        fingerprint reflects only processed materials.

        The node's ``materials`` and ``children`` relationships must
        be loaded before calling this method.

        Issues a single flush after the entire subtree is computed
        (no flush on cache hit).

        Args:
            node: A loaded CourseNode ORM instance with eagerly
                loaded ``materials`` and ``children``.

        Returns:
            64-char lowercase hex SHA-256 digest.
        """
        if node.content_hash is not None:
            return node.content_hash
        fp = self._compute_node_fp(node)
        await self._session.flush()
        return fp

    async def ensure_course_fp(self, root_nodes: list[CourseNode]) -> str:
        """Compute Merkle fingerprint for an entire course.

        Combines root-node fingerprints into a sorted list and hashes
        them. Each root node's subtree is computed via
        ``_compute_node_fp`` (bottom-up), then a single flush persists
        all computed fingerprints.

        Not stored in DB — computed on the fly from root nodes.

        Args:
            root_nodes: Root-level CourseNode instances
                (``parent_id is None``) with eagerly loaded subtrees.

        Returns:
            64-char lowercase hex SHA-256 digest.
        """
        parts: list[str] = []
        for node in root_nodes:
            fp = self._compute_node_fp(node)
            parts.append(fp)
        parts.sort()
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        await self._session.flush()
        return digest

    async def invalidate_up(self, node: CourseNode) -> None:
        """Clear content_hash from ``node`` up to the root.

        Walks the parent chain using ``parent_id``, setting
        ``content_hash = None`` on every ancestor. Issues a
        single flush after the full walk.

        Safe in async context — loads each parent via
        ``session.get()`` instead of accessing the lazy
        ``node.parent`` relationship.

        Args:
            node: The starting node (e.g. node whose material changed).
        """
        current: CourseNode | None = node
        while current is not None:
            current.content_hash = None
            if current.parent_id is None:
                break
            current = await self._session.get(CourseNode, current.parent_id)
        await self._session.flush()

    # ── Internal compute (no flush) ──

    @staticmethod
    def _compute_material_fp(entry: AuthoredDocument) -> str:
        """Return ``content_hash`` as the material fingerprint.

        Raises:
            ValueError: If ``content_hash`` is None (not yet processed).
        """
        if entry.content_hash is not None:
            return entry.content_hash

        msg = (
            f"Cannot compute fingerprint: AuthoredDocument {entry.id} "
            f"has no content_hash"
        )
        raise ValueError(msg)

    def _compute_node_fp(self, node: CourseNode) -> str:
        """Compute Merkle fingerprint for a node without flushing.

        Recurses into children (bottom-up), then combines all parts.
        """
        if node.content_hash is not None:
            return node.content_hash

        parts: list[str] = []

        # Material fingerprints (skip unprocessed)
        for mat in node.documents:
            if mat.content_hash is not None:
                fp = self._compute_material_fp(mat)
                parts.append(f"m:{fp}")

        # Child node fingerprints (recursive)
        for child in node.children:
            fp = self._compute_node_fp(child)
            parts.append(f"n:{fp}")

        parts.sort()
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        node.content_hash = digest
        return digest
