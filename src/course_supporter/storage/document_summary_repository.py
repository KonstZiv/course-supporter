"""Repository for DocumentSummary CRUD + cascade content_hash (KD-2.1-F).

Pass 2a (Phase 2.1 commit 5) materialises a single DocumentSummary
per AuthoredDocument by calling :meth:`DocumentSummaryRepository.create`.
The repository owns the cascade-invalidation discipline of KD-2.1-F:
``ContentHashService.invalidate_up(summary)`` runs inside ``create()``
after the INSERT flush, so every caller that creates a summary
automatically propagates fresh ``content_hash`` values up the parent
chain (AuthoredDocument -> CourseNode -> ... -> root).

Standalone class (not a SoftDeleteRepository subclass) per Phase 2.1
C5 ratify item 7.B: matches the 9/9 existing repository convention
established in ``authored_document_repository.py`` / ``job_repository.py``
/ ``course_node_repository.py`` etc. A future phase may migrate all
repositories onto :class:`SoftDeleteRepository` inheritance with full
blast-radius assessment; Phase 2.1 is not that phase.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.cascade import (
    CascadeDeleteService,
    build_cascade_map,
)
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import AuthoredDocument, DocumentSummary


class DocumentSummaryRepository:
    """Repository for DocumentSummary creation, lookup, and lifecycle.

    Not tenant-scoped at the repository level -- DocumentSummary
    inherits tenant isolation through ``authored_document_id``
    (UNIQUE 1:1 per KD-theta.2) and ``course_root_id`` (KD-delta
    denormalised root pointer).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, summary_id: uuid.UUID) -> DocumentSummary | None:
        """Fetch a DocumentSummary by primary key."""
        stmt = select(DocumentSummary).where(DocumentSummary.id == summary_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_authored_document_id(
        self, authored_document_id: uuid.UUID
    ) -> DocumentSummary | None:
        """Fetch the ACTIVE DocumentSummary attached to a given AuthoredDocument.

        Filters ``deleted_at IS NULL`` (Task 3.2.6 Finding 2): the 1:1
        invariant is now an ACTIVE-only invariant enforced by a PARTIAL
        unique index (one active summary per document), so soft-deleted
        history may coexist with the live row. ``scalar_one_or_none`` would
        otherwise raise ``MultipleResultsFound`` once a reprocess leaves a
        soft-deleted predecessor behind. Both consumers — the on_failure
        orphan-observer and the reprocess release-lookup — want the active
        row, never the soft-deleted history.
        """
        stmt = select(DocumentSummary).where(
            DocumentSummary.authored_document_id == authored_document_id,
            DocumentSummary.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        authored_document_id: uuid.UUID,
        title: str,
        description: str,
        main_concepts: list[str],
        secondary_concepts: list[str],
        content_char_count: int,
        structure: dict[str, Any] | None = None,
    ) -> DocumentSummary:
        """Insert a new DocumentSummary and cascade-invalidate content_hash.

        Derives ``course_root_id`` from the parent ``AuthoredDocument``
        (KD-delta denormalised root). Sets ``status='ready'``: Pass 2a
        materialises the summary immediately upon successful LLM mapping;
        the legacy ``'pending'`` lifecycle state is bypassed by the
        text/web direct-create flow.

        After INSERT + flush, invokes
        :meth:`ContentHashService.invalidate_up` on the new summary,
        which walks up the parent chain (AuthoredDocument -> CourseNode
        -> ... -> root) recomputing ``content_hash``. This closes
        KD-2.1-F: every DocumentSummary creation propagates a fresh
        Merkle hash to the root without requiring callers to remember
        the cascade step.

        Raises:
            ValueError: If ``authored_document_id`` does not exist.
        """
        parent = await self._session.get(AuthoredDocument, authored_document_id)
        if parent is None:
            msg = f"AuthoredDocument not found: {authored_document_id}"
            raise ValueError(msg)

        # Task 3.2.6 Finding 2: release any ACTIVE prior summary before the
        # INSERT so the partial-active unique (one active summary per
        # document) holds on reprocess. Owned-by-repo (mirrors the
        # cascade-invalidate discipline below). The soft-delete cascades to
        # the old segments via DocumentSummary.__cascades_soft_delete_to__.
        # No hash hook here: the post-INSERT invalidate_up recomputes the
        # parent chain from the now-active new summary, and the hash-walk
        # excludes soft-deleted children (content_hash.py:381-383), so a
        # single correct invalidation flows from the post-release state.
        # First-time create has no active prior → release is skipped.
        #
        # FLUSH-ORDERING IS LOAD-BEARING. The release must soft-delete AND
        # flush the old row BEFORE the new INSERT. SQLAlchemy's unit of work,
        # left to itself, flushes INSERTs ahead of UPDATEs of the same class
        # — so if the soft-delete UPDATE and the new INSERT shared one
        # uninterrupted flush, the INSERT would fire while the old row is
        # still active (deleted_at IS NULL), yielding two active rows and a
        # UniqueViolation on the partial-active index. The ordering holds
        # ONLY because CascadeDeleteService.soft_delete_with_cascade flushes
        # the soft-delete here, before the create-INSERT below. Do NOT move
        # this release after ``session.add(summary)``; do NOT make the
        # cascade flush lazy. Locked by the reprocess no-UniqueViolation
        # integration test (test_reprocess_soft_deletes_old_and_inserts_new).
        old = await self.get_by_authored_document_id(authored_document_id)
        if old is not None:
            await CascadeDeleteService(self._session).soft_delete_with_cascade(
                old, build_cascade_map(DocumentSummary)
            )

        summary = DocumentSummary(
            authored_document_id=authored_document_id,
            course_root_id=parent.course_root_id,
            title=title,
            description=description,
            main_concepts=main_concepts,
            secondary_concepts=secondary_concepts,
            content_char_count=content_char_count,
            # task-code-materials: project tree for code materials; None for
            # every other source type, which is what makes the author read
            # surface (GET /documents/{id}/structure) able to 404 on it.
            structure=structure,
            status="ready",
        )
        self._session.add(summary)
        await self._session.flush()
        # KD-2.1-F: cascade content_hash up through parent chain.
        # ContentHashService dispatches on DocumentSummary explicitly
        # at content_hash.py:341 (compute_local_hash) + 408
        # (compute_local_hash dispatcher).
        await ContentHashService(self._session).invalidate_up(summary)
        return summary
