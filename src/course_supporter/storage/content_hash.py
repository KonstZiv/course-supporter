"""Content hash service (vision §3 KD9).

Implements the KD9 hash model: ``raw_hash`` (SHA-256 of authored
input bytes) and ``content_hash`` (Merkle SHA-256 over local content
plus sorted child hashes). Hashes anchor change detection across
the material tree — a regenerated DocumentSegment changes its own
hash, which propagates up through DocumentSummary → CourseNode all
the way to the root.

KD9 explicitly forbids lazy / on-read materialisation (vision §3
line 563): hashes must be computed and persisted at INSERT/UPDATE
time. This service is the canonical KD9 implementation; the legacy
half-aligned fingerprint service (anchored on
``AuthoredDocument.processed_hash``) was retired in Phase 1.1
(commits 1.1.C1 through 1.1.C3).

**Per-entity formulas in 0.2 are minimal-viable.** The compute
helpers (``compute_raw_hash`` / ``compute_content_hash``) ship the
algebra; ``ContentHashService._compute_hash_for`` ships a basic
formula per entity type that is enough to make
``invalidate_up`` walk the tree correctly and short-circuit on
no-op changes. Phase 1.3 / 1.4 / 2 / 3 refine each formula with
vision-aligned inputs (concepts, positions, additional fields).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Protocol, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
)

logger = structlog.get_logger(__name__)

# Separator byte for the Merkle hash. Without an explicit boundary,
# ``["abc", "def"]`` would hash identically to ``["abcd", "ef"]``.
# The NUL byte cannot appear inside a hex SHA-256 digest, so it is
# a safe, unambiguous delimiter that no child hash payload can spoof.
_HASH_SEPARATOR = b"\x00"

# Defensive cap on walker depth. Real course trees are shallow (a
# handful of levels), so 25 is comfortable headroom; the cap exists
# to surface accidentally-introduced cycles in future schema changes
# rather than to bound legitimate work.
_MAX_WALK_DEPTH = 25


class HashableEntity(Protocol):
    """Structural type for any model that participates in content_hash.

    Concrete hash-bearing models (``DocumentSegment``,
    ``DocumentSummary``, ``AuthoredDocument``, ``CourseNode``)
    all expose these three attributes — ``id`` and ``deleted_at``
    come from existing model contracts and ``content_hash`` is the
    column added by task 0.2.
    """

    id: uuid.UUID
    deleted_at: datetime | None
    content_hash: str | None


def compute_raw_hash(content: bytes) -> str:
    """SHA-256 hex digest of the raw authored bytes (vision §3 KD9).

    Used for ``AuthoredDocument.raw_hash`` (currently named
    ``AuthoredDocument.raw_hash`` until the Phase 1.2 rename). Once a
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


def _encode_local_fields(payload: dict[str, object]) -> bytes:
    """Stable byte encoding of an entity's local-content fields.

    Returns deterministic UTF-8 JSON bytes via ``json.dumps`` with
    ``sort_keys=True`` and ``separators=(",", ":")`` — keys lay out
    identically across calls regardless of insertion order, no
    superfluous whitespace contributes to the hash, and ``ensure_ascii=
    False`` keeps Unicode strings in their canonical form so the same
    course content from two providers (e.g. UA vs RU diacritic
    normalization) does not produce divergent hashes downstream.

    Concept lists are sorted before being added to ``payload`` (LLM
    output order is non-deterministic; stable hashing requires order
    normalization at the formula boundary). Concept fields are passed
    through here as-is — the caller is responsible for the ``sorted(...)``.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class ContentHashService:
    """Materialised KD9 content-hash maintenance.

    Walks the material tree bottom-up, recomputing ``content_hash``
    on each ancestor and short-circuiting when a level's recomputed
    value equals the stored value (so a metadata-only change on a
    leaf does not write-storm up the tree). All updates happen
    in-session; the caller controls the surrounding transaction.

    Per-entity formulas live in :meth:`_compute_hash_for` and are
    minimal-viable for 0.2 — Phase 1.3 / 1.4 / 2 / 3 will refine
    each one with vision-aligned inputs. ``_compute_hash_for`` and
    :meth:`_fetch_parent` are deliberately overrideable so unit
    tests can drive the walker against in-memory fakes without a
    DB connection.

    The service is stateless aside from the bound session; callers
    construct it inline per request / job, there is no singleton.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def invalidate_up(self, entity: HashableEntity) -> None:
        """Eager Merkle propagation up the tree (vision §3 KD9).

        Recomputes ``content_hash`` for ``entity`` from its active
        children, then walks to the parent and repeats. Stops at
        a root node (parent IS NULL) or as soon as a level's
        recomputed hash equals the stored hash (short-circuit; no
        write storm on metadata-only changes).

        Soft-deleted entities encountered along the walk are skipped
        for UPDATE — the soft-delete trigger from task 0.1 would
        block the write — but their parent is still visited so the
        parent's hash reflects active siblings.

        Issues a single ``flush()`` after the entire walk completes
        (no per-level flush). Caller's transaction boundary is
        unchanged.
        """
        visited: set[tuple[type, uuid.UUID]] = set()
        await self._walk_up(entity, visited, exclude_ids=None)
        await self._session.flush()

    async def invalidate_subtree(
        self,
        entity_ids: list[uuid.UUID],
        *,
        exclude_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Bulk Merkle propagation for cascade-soft-delete scenarios.

        Looks up entities by id across the four hash-bearing tables
        (``DocumentSegment``, ``DocumentSummary``,
        ``AuthoredDocument``, ``CourseNode``) — one ``WHERE id IN``
        query per type, four total round-trips regardless of
        ``len(entity_ids)``. For each entity, walks up to the root
        with a *shared* ``visited`` set so a parent reached from N
        siblings is recomputed exactly once.

        ``exclude_ids`` lets the caller supply ids that should be
        treated as "already gone" by parent computations. The
        primary use case is the ``on_invalidate_hashes`` cascade
        hook (per task 0.1 A3): it fires *before* writes, so cascade
        victims still have ``deleted_at IS NULL`` at hook time.
        Passing ``exclude_ids = {victim ids}`` makes parent hashes
        recompute as if the victims were already deleted, which is
        the correct cascade-time semantics. Without ``exclude_ids``
        the standalone semantics apply (recompute from current
        ``deleted_at IS NULL`` state).

        Issues a single ``flush()`` after the full bulk walk.
        Empty ``entity_ids`` short-circuits without DB access.
        """
        if not entity_ids:
            return

        entities: list[HashableEntity] = []
        for cls in (
            DocumentSegment,
            DocumentSummary,
            AuthoredDocument,
            CourseNode,
        ):
            stmt = select(cls).where(cls.id.in_(entity_ids))
            result = await self._session.execute(stmt)
            # ORM rows satisfy ``HashableEntity`` structurally (every
            # hash-bearing model has id + deleted_at + content_hash),
            # but Protocol variance through ``Sequence[Base]`` is not
            # inferred — explicit cast for the type checker.
            entities.extend(cast(list[HashableEntity], list(result.scalars().all())))

        visited: set[tuple[type, uuid.UUID]] = set()
        for entity in entities:
            await self._walk_up(entity, visited, exclude_ids=exclude_ids)

        await self._session.flush()

    async def _walk_up(
        self,
        start: HashableEntity,
        visited: set[tuple[type, uuid.UUID]],
        *,
        exclude_ids: set[uuid.UUID] | None,
    ) -> None:
        """Walk from ``start`` to the root, recomputing hashes as we go.

        Skips UPDATE on entities that are either soft-deleted (the
        trigger would block) or in ``exclude_ids`` (caller asked us
        to treat them as gone). In both cases we still walk to the
        parent so its hash reflects the absence. Cycle-safe via
        ``visited``; depth-capped at :data:`_MAX_WALK_DEPTH`.
        """
        current: HashableEntity | None = start
        depth = 0
        while current is not None:
            if depth > _MAX_WALK_DEPTH:
                logger.warning(
                    "content_hash.invalidate_up.depth_exceeded",
                    entity_type=type(current).__name__,
                    entity_id=str(current.id),
                    max_depth=_MAX_WALK_DEPTH,
                )
                return

            depth += 1
            key = (type(current), current.id)
            if key in visited:
                return
            visited.add(key)

            should_update = current.deleted_at is None and (
                exclude_ids is None or current.id not in exclude_ids
            )
            if should_update:
                new_hash = await self._compute_hash_for(
                    current, exclude_ids=exclude_ids
                )
                if new_hash == current.content_hash:
                    return
                current.content_hash = new_hash

            current = await self._fetch_parent(current)

    async def _compute_hash_for(
        self,
        entity: HashableEntity,
        *,
        exclude_ids: set[uuid.UUID] | None,
    ) -> str:
        """Per-entity hash formula (vision §3 KD9).

        Phase 1 commit (e) extends ``DocumentSegment`` and
        ``DocumentSummary`` with own content fields per Gap 2 of the
        sprint deferred debt; ``AuthoredDocument`` and ``CourseNode``
        formulas are unchanged here (Phase 2 / Phase 3 territory).
        The current shapes:

        - ``DocumentSegment`` — local content covers ``content`` plus
          sorted ``main_concepts`` + sorted ``secondary_concepts``;
          no children. ``content_char_count`` is excluded as a derived
          field (= ``len(content)``) and would double-count the same
          input. FK references and timestamps are excluded.
        - ``DocumentSummary`` — local content covers ``title`` +
          ``description`` + sorted ``main_concepts`` + sorted
          ``secondary_concepts``; children = sorted active segments'
          ``content_hash`` (Merkle aggregation per KD9). Same exclusion
          rules as ``DocumentSegment``.
        - ``AuthoredDocument`` — ``raw_hash`` bytes + sorted active
          summaries' ``content_hash``. ``raw_hash`` anchor matches
          KD9 line 554. Falls back to ``b""`` while ``raw_hash`` is
          NULL (Phase 2 will populate it at ingestion time).
        - ``CourseNode`` — empty local content + sorted (active
          documents' + active child nodes') ``content_hash``. Phase
          1.1 collapses ``node_fingerprint`` into ``content_hash``
          and switches to the full KD9 formula.

        Concept list ordering is normalized via ``sorted(...)`` at the
        formula boundary because LLM output order is non-deterministic;
        without normalization a producer reshuffle would spuriously
        invalidate every downstream Merkle ancestor.

        ``exclude_ids`` is forwarded to the children query so the
        cascade hook can elide soon-to-be-deleted siblings before
        they actually have ``deleted_at`` set.
        """
        if isinstance(entity, DocumentSegment):
            local = _encode_local_fields(
                {
                    "content": entity.content or "",
                    "main_concepts": sorted(entity.main_concepts or []),
                    "secondary_concepts": sorted(entity.secondary_concepts or []),
                }
            )
            return compute_content_hash(local, [])

        if isinstance(entity, DocumentSummary):
            local = _encode_local_fields(
                {
                    "title": entity.title or "",
                    "description": entity.description or "",
                    "main_concepts": sorted(entity.main_concepts or []),
                    "secondary_concepts": sorted(entity.secondary_concepts or []),
                }
            )
            stmt = select(DocumentSegment.content_hash).where(
                DocumentSegment.document_summary_id == entity.id,
                DocumentSegment.deleted_at.is_(None),
            )
            if exclude_ids:
                stmt = stmt.where(DocumentSegment.id.notin_(exclude_ids))
            result = await self._session.execute(stmt)
            children = [h for (h,) in result.all() if h is not None]
            return compute_content_hash(local, children)

        if isinstance(entity, AuthoredDocument):
            local = (entity.raw_hash or "").encode("ascii")
            stmt = select(DocumentSummary.content_hash).where(
                DocumentSummary.authored_document_id == entity.id,
                DocumentSummary.deleted_at.is_(None),
            )
            if exclude_ids:
                stmt = stmt.where(DocumentSummary.id.notin_(exclude_ids))
            result = await self._session.execute(stmt)
            children = [h for (h,) in result.all() if h is not None]
            return compute_content_hash(local, children)

        if isinstance(entity, CourseNode):
            entry_stmt = select(AuthoredDocument.content_hash).where(
                AuthoredDocument.course_node_id == entity.id,
                AuthoredDocument.deleted_at.is_(None),
            )
            if exclude_ids:
                entry_stmt = entry_stmt.where(AuthoredDocument.id.notin_(exclude_ids))
            entry_result = await self._session.execute(entry_stmt)

            node_stmt = select(CourseNode.content_hash).where(
                CourseNode.parent_id == entity.id,
                CourseNode.deleted_at.is_(None),
            )
            if exclude_ids:
                node_stmt = node_stmt.where(CourseNode.id.notin_(exclude_ids))
            node_result = await self._session.execute(node_stmt)

            children = [h for (h,) in entry_result.all() if h is not None]
            children += [h for (h,) in node_result.all() if h is not None]
            return compute_content_hash(b"", children)

        raise TypeError(
            f"ContentHashService does not support entity type {type(entity).__name__}"
        )

    async def _fetch_parent(self, entity: HashableEntity) -> HashableEntity | None:
        """Resolve ``entity``'s parent in the hash chain, or None at root.

        - Segment → MacroSection (via ``document_summary_id``)
        - MacroSection → AuthoredDocument (via ``authored_document_id``)
        - AuthoredDocument → CourseNode (via ``course_node_id``)
        - CourseNode → CourseNode (via ``parent_id``);
          NULL = root, walk terminates.
        """
        if isinstance(entity, DocumentSegment):
            return await self._session.get(DocumentSummary, entity.document_summary_id)
        if isinstance(entity, DocumentSummary):
            return await self._session.get(
                AuthoredDocument, entity.authored_document_id
            )
        if isinstance(entity, AuthoredDocument):
            return await self._session.get(CourseNode, entity.course_node_id)
        if isinstance(entity, CourseNode):
            if entity.parent_id is None:
                return None
            return await self._session.get(CourseNode, entity.parent_id)
        raise TypeError(
            f"ContentHashService does not support entity type {type(entity).__name__}"
        )
