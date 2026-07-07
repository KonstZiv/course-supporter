"""Repository for ProjectBase — append-only base-archive versions (KD18 P2).

Not tenant-scoped — tenant isolation is ensured at the API layer (the base
belongs to an ``AuthoredDocument`` whose tenant is resolved by the route). The
repository flushes but never commits: the caller (the base-attach route, via
the QQ5 enqueue helper) owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import ProjectBase


class ProjectBaseRepository:
    """CRUD + lifecycle for ``project_bases`` (KD18 P2)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_version(
        self,
        *,
        authored_document_id: uuid.UUID,
        archive_key: str,
    ) -> ProjectBase:
        """Create the next ``pending`` base version for a document.

        Version allocation is ``MAX(version) + 1`` computed INSIDE the
        caller's transaction (mirrors ``AuthoredDocumentRepository``'s
        ``_next_sibling_order``). The composite ``UNIQUE(authored_document_id,
        version)`` is the concurrency arbiter, NOT the read: two racing
        re-uploads of the same document read the same ``MAX + 1`` and the
        second ``flush`` raises ``IntegrityError`` (surfaced to the route for
        a clean 409, not swallowed here). This is deliberately simpler than a
        ``SELECT ... FOR UPDATE`` — base re-upload is a rare author action and
        the unique index already guarantees no duplicate version is ever
        persisted.

        Snapshot columns stay NULL and ``state`` stays ``pending`` until the
        base-normalize ARQ job flips it. Flushes; does not commit.
        """
        next_version = await self._next_version(authored_document_id)
        base = ProjectBase(
            authored_document_id=authored_document_id,
            version=next_version,
            archive_key=archive_key,
            state="pending",
        )
        self._session.add(base)
        await self._session.flush()
        return base

    async def get_by_id(self, base_id: uuid.UUID) -> ProjectBase | None:
        """Load a base version by id (identity-map cache hit if already loaded)."""
        return await self._session.get(ProjectBase, base_id)

    async def get_latest(self, authored_document_id: uuid.UUID) -> ProjectBase | None:
        """Latest version for the document REGARDLESS of state.

        Backs ``GET /base``'s fallback arm (Decision 8): when no version is
        READY yet, the caller still sees the latest row's ``state`` (pending /
        failed) rather than a 404.
        """
        stmt = (
            select(ProjectBase)
            .where(ProjectBase.authored_document_id == authored_document_id)
            .order_by(ProjectBase.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_ready(
        self, authored_document_id: uuid.UUID
    ) -> ProjectBase | None:
        """Latest READY version — the active base (KD18 'active = latest READY').

        Distinct from :meth:`get_latest`: with a READY v1 and a later pending
        v2, this returns v1 while ``get_latest`` returns v2. Backs the primary
        arm of ``GET /base`` and the author manifest-read (READY-only).
        """
        stmt = (
            select(ProjectBase)
            .where(
                ProjectBase.authored_document_id == authored_document_id,
                ProjectBase.state == "ready",
            )
            .order_by(ProjectBase.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_snapshot_hash(
        self, authored_document_id: uuid.UUID, snapshot_hash: str
    ) -> ProjectBase | None:
        """Echo-match: the base version whose snapshot the student diffed against.

        The KD18 P3 echo contract — a project submission carries the
        ``base_snapshot_hash`` it was built from; this resolves it to the exact
        base version. Scoped to the ONE document and matched against ANY of its
        versions (not just the active/latest): a student who built on v1 while v3
        is active still resolves to v1 (staleness surfaces separately on the
        read-path from ``version``). ``snapshot_hash`` is NULL until READY, so
        pending / failed versions never match; the explicit ``state == 'ready'``
        filter keeps the intent legible.

        Returns the full row (``id`` → the submission's ``base_id``, ``version``
        → staleness, ``manifest`` → delta) or None when the hash matches no
        version — the caller (Commit 4) treats None as an unknown echo → 422.
        Ordered ``version`` DESC + LIMIT 1 so a byte-identical re-upload (two
        versions sharing a hash) resolves to the newest match (minimal
        staleness), deterministically.
        """
        stmt = (
            select(ProjectBase)
            .where(
                ProjectBase.authored_document_id == authored_document_id,
                ProjectBase.snapshot_hash == snapshot_hash,
                ProjectBase.state == "ready",
            )
            .order_by(ProjectBase.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_ready(
        self,
        base_id: uuid.UUID,
        *,
        snapshot_key: str,
        snapshot_hash: str,
        manifest: dict[str, Any],
    ) -> ProjectBase:
        """One-shot ``pending → ready``: persist the normalized snapshot.

        The update-state pair (with :meth:`mark_failed`). Append-only, called
        exactly once per version by the base-normalize job from ``pending``;
        there is no ``ready → pending`` re-open (re-upload creates a NEW
        version). Does NOT enforce the transition (the CHECK guards only the
        value set) — the job is the sole caller. Flushes; does not commit.

        Raises:
            ValueError: the base version does not exist.
        """
        base = await self._session.get(ProjectBase, base_id)
        if base is None:
            msg = f"ProjectBase not found: {base_id}"
            raise ValueError(msg)
        base.snapshot_key = snapshot_key
        base.snapshot_hash = snapshot_hash
        base.manifest = manifest
        base.failure_reason = None
        base.state = "ready"
        await self._session.flush()
        return base

    async def mark_failed(
        self,
        base_id: uuid.UUID,
        *,
        failure_reason: str,
    ) -> ProjectBase:
        """One-shot ``pending → failed``: record the human-readable reason.

        The update-state pair (with :meth:`mark_ready`). Same append-only,
        no-transition-enforcement contract. Flushes; does not commit.

        Raises:
            ValueError: the base version does not exist.
        """
        base = await self._session.get(ProjectBase, base_id)
        if base is None:
            msg = f"ProjectBase not found: {base_id}"
            raise ValueError(msg)
        base.failure_reason = failure_reason
        base.state = "failed"
        await self._session.flush()
        return base

    async def _next_version(self, authored_document_id: uuid.UUID) -> int:
        """Next 1-based version for the document (``MAX(version) + 1``)."""
        stmt = select(func.coalesce(func.max(ProjectBase.version) + 1, 1)).where(
            ProjectBase.authored_document_id == authored_document_id
        )
        return (await self._session.execute(stmt)).scalar_one()
