"""Repository for DocumentSegment batch CRUD + cascade content_hash (KD-2.1-F).

Pass 2b (Phase 2.1 commit 7) materialises ``DocumentSegment`` rows from
the upstream ``DocumentSegmentDraft`` list via
:meth:`DocumentSegmentRepository.create_batch`. The repository owns the
cascade-invalidation discipline of KD-2.1-F symmetric to
``DocumentSummaryRepository.create``: after the batch INSERT + flush,
``ContentHashService.invalidate_up(summary)`` propagates fresh
``content_hash`` values from each new segment up through the parent
``DocumentSummary`` -> ``AuthoredDocument`` -> ``CourseNode`` chain to
the root.

Standalone class (not a SoftDeleteRepository subclass) per the existing
repository convention (matches ``DocumentSummaryRepository`` shape).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.ingestion.base import ProcessingError
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import DocumentSegment, DocumentSummary

if TYPE_CHECKING:
    from course_supporter.ingestion.schemas import DocumentSegmentDraft
    from course_supporter.models.source import SourceDocument


class DocumentSegmentRepository:
    """Repository for DocumentSegment batch creation + cascade hash.

    Not tenant-scoped at the repository level -- DocumentSegment inherits
    tenant isolation through ``document_summary_id`` and ``course_root_id``
    (KD-delta denormalised root pointer).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self,
        document_summary_id: uuid.UUID,
        segment_drafts: list[DocumentSegmentDraft],
        *,
        source_doc: SourceDocument,
    ) -> list[DocumentSegment]:
        """Insert N DocumentSegment rows and cascade-invalidate content_hash.

        For each draft:

        - If ``draft.content`` is non-None (audio/video pipelines that
          fill content in Pass 2b directly), use it verbatim.
        - If ``draft.content`` is ``None`` (text/web canonical path per
          KD-2.1-O), slice from ``source_doc.assemble_text()`` using
          ``draft.start_pos`` / ``draft.end_pos``. The reference text is
          identical to what Pass 2a's mapping LLM saw -- offsets line up
          exactly.

        Offsets are bounds-checked against the resolved reference text
        before slicing; out-of-range drafts raise ``ProcessingError``
        rather than silently emitting an empty or wrong segment.

        After ``add_all`` + ``flush``, walks each segment up the parent
        chain via :meth:`ContentHashService.invalidate_up`. The per-segment
        walk pattern (vs single ``invalidate_up(summary)``) is intentional:
        the summary's hash query filters NULL ``content_hash`` children
        (``content_hash.py:357``), so we need every leaf to carry its hash
        before the summary recompute reads them. End state is correct on
        the last segment's walk; intermediate ancestor updates are
        in-session and committed atomically by the caller.

        Args:
            document_summary_id: Parent ``DocumentSummary.id``.
            segment_drafts: Pass 2a-emitted drafts (offsets + concepts
                + optional title/description + optional content).
            source_doc: Source document carrying ``assemble_text()`` for
                the slicing fallback path.

        Returns:
            List of persisted ``DocumentSegment`` instances in input order.

        Raises:
            ValueError: If ``document_summary_id`` does not exist.
            ProcessingError: If any draft's offsets violate
                ``0 <= start_pos < end_pos <= len(reference_text)``.
        """
        summary = await self._session.get(DocumentSummary, document_summary_id)
        if summary is None:
            msg = f"DocumentSummary not found: {document_summary_id}"
            raise ValueError(msg)

        reference_text = source_doc.assemble_text()
        ref_len = len(reference_text)

        segments: list[DocumentSegment] = []
        for draft in segment_drafts:
            if not (0 <= draft.start_pos < draft.end_pos <= ref_len):
                msg = (
                    f"Segment offsets out of bounds for draft order={draft.order}: "
                    f"start_pos={draft.start_pos}, end_pos={draft.end_pos}, "
                    f"reference_text length={ref_len}"
                )
                raise ProcessingError(msg)

            content = (
                draft.content
                if draft.content is not None
                else reference_text[draft.start_pos : draft.end_pos]
            )
            segments.append(
                DocumentSegment(
                    document_summary_id=summary.id,
                    course_root_id=summary.course_root_id,
                    order=draft.order,
                    start_pos=draft.start_pos,
                    end_pos=draft.end_pos,
                    title=draft.title,
                    description=draft.description,
                    content=content,
                    content_char_count=len(content),
                    main_concepts=list(draft.main_concepts),
                    secondary_concepts=list(draft.secondary_concepts),
                )
            )

        if not segments:
            return segments

        self._session.add_all(segments)
        await self._session.flush()

        hash_service = ContentHashService(self._session)
        for seg in segments:
            await hash_service.invalidate_up(seg)

        return segments
