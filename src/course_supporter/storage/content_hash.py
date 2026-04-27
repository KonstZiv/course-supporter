"""Content hash service (vision §3 KD9).

Implements the KD9 hash model: ``raw_hash`` (SHA-256 of authored
input bytes) and ``content_hash`` (Merkle SHA-256 over local content
plus sorted child hashes). Hashes anchor change detection across
the material tree — a regenerated DocumentSegment changes its own
hash, which propagates up through DocumentSummary → CourseNode all
the way to the root.

This module ships the pure compute helpers in commit (a) of task
0.2; the DB-aware traversal API (``invalidate_up`` /
``invalidate_subtree``) lands in commit (c) once the column
additions in commit (b) are in place.

KD9 explicitly forbids lazy / on-read materialisation (vision §3
line 563): hashes must be computed and persisted at INSERT/UPDATE
time. The legacy ``FingerprintService`` (anchored on
``MaterialEntry.processed_hash``) remains in place for the
snapshot / reconciliation flows it serves and is collapsed into
``ContentHashService`` in Phase 1.1.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

# Separator byte for the Merkle hash. Without an explicit boundary,
# ``["abc", "def"]`` would hash identically to ``["abcd", "ef"]``.
# The NUL byte cannot appear inside a hex SHA-256 digest, so it is
# a safe, unambiguous delimiter that no child hash payload can spoof.
_HASH_SEPARATOR = b"\x00"


def compute_raw_hash(content: bytes) -> str:
    """SHA-256 hex digest of the raw authored bytes (vision §3 KD9).

    Used for ``AuthoredDocument.raw_hash`` (currently named
    ``MaterialEntry.raw_hash`` until the Phase 1.2 rename). Once a
    value is persisted it is treated as immutable: re-uploading the
    same document with different bytes is a new authored input that
    invalidates downstream summaries (vision §3 KD4).

    Returns a 64-character lowercase hex string.
    """
    return hashlib.sha256(content).hexdigest()


def compute_content_hash(local_content: bytes, child_hashes: list[str]) -> str:
    """Merkle SHA-256 hex digest combining local content with child hashes.

    Children are sorted before hashing so the result is independent
    of insertion order (re-ordering siblings does not invalidate the
    parent). Each child hash is separated by ``\\x00`` so that two
    different child partitions cannot collide via concatenation —
    without the separator, ``["abc", "def"]`` would hash identically
    to ``["abcd", "ef"]``. The NUL byte cannot appear inside a hex
    SHA-256 digest, which makes it a safe, unambiguous boundary.

    Used for: ``DocumentSegment.content_hash`` (local_content =
    canonical bytes of the segment, ``child_hashes = []``);
    ``DocumentSummary.content_hash`` (children = sorted segment
    ``content_hash`` values); ``CourseNode.content_hash`` (children =
    sorted ``AuthoredDocument.raw_hash`` + their
    ``DocumentSummary.content_hash`` + child ``CourseNode.content_hash``
    values). Per-entity decisions about what bytes count as
    "local content" are wired in Phase 2 / Phase 3; this helper
    just enforces the algebra.

    Returns a 64-character lowercase hex string.
    """
    hasher = hashlib.sha256()
    hasher.update(local_content)
    for child in sorted(child_hashes):
        hasher.update(_HASH_SEPARATOR)
        hasher.update(child.encode("ascii"))
    return hasher.hexdigest()


class ContentHashService:
    """Materialised KD9 content-hash maintenance.

    Commit (a) of task 0.2 ships this class as a thin shell so the
    pure compute helpers above can be imported and the DB-aware API
    can attach in commit (c) without re-introducing the class.
    Commit (c) adds:

    - ``invalidate_up(session, entity)`` — eager bottom-up walk that
      recomputes each ancestor's ``content_hash`` from its children
      and short-circuits when a level's recomputed hash equals the
      stored hash.
    - ``invalidate_subtree(session, entity_ids)`` — bulk variant for
      cascade-soft-delete scenarios; deduplicates ancestor walks via
      a ``visited`` set so a parent reached from multiple siblings
      is recomputed exactly once.

    The service is stateless aside from the bound session. Callers
    construct it inline per request / job; there is no global
    singleton.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
