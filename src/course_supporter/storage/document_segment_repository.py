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

Phase 3.3b adds the read side: :meth:`DocumentSegmentRepository
.search_by_concepts` answers "where in the course is concept X
explained?" (vision §4) via JSONB ``@>`` containment over the
GIN-indexed concept columns (KD-gamma), resolving each hit's lineage
(material + node names) in a single JOIN so the Mentor agent does not
dereference names one-by-one.

Standalone class (not a SoftDeleteRepository subclass) per the existing
repository convention (matches ``DocumentSummaryRepository`` shape).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Row, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.ingestion.base import ProcessingError
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
)

if TYPE_CHECKING:
    from course_supporter.ingestion.schemas import DocumentSegmentDraft
    from course_supporter.models.source import SourceDocument


@dataclass(frozen=True, slots=True)
class SegmentAnchor:
    """Positional anchor of a segment within its source material (Phase 3.3a).

    ``kind`` is derived from the material's ``source_type`` (not from which
    numeric column pair happens to be populated), so it is *always* defined:

    - ``"time"`` — video/audio, seconds (``float``): ``start_time_sec`` /
      ``end_time_sec``.
    - ``"slide"`` — presentation, slide numbers (``int``): ``start_slide`` /
      ``end_slide``.
    - ``"paragraph"`` — text/web, paragraph ordinals (``int``; chunk-level
      coarse address per DD-3.3a-A): ``start_paragraph`` / ``end_paragraph``.
    - ``"file"`` — code (task-code-materials), the source-file path: a
      SINGLE value in :attr:`file_path` (one included file = one segment,
      so there is no start/end pair; the numeric bounds stay ``None``).

    ``start`` / ``end`` may still be ``None`` -- notably for a text/web
    segment whose paragraph ordinal could not be computed (KD-C empty
    anchor): the anchor is ``("paragraph", None, None)``, so the consumer
    still knows it is a text material even without exact bounds.
    ``file_path`` defaults to ``None`` so the existing positional
    3-argument constructions stay valid for the three numeric kinds.
    """

    kind: Literal["time", "slide", "paragraph", "file"]
    start: float | int | None
    end: float | int | None
    file_path: str | None = None


@dataclass(frozen=True, slots=True)
class ConceptSearchHit:
    """One pointer-hit of a DocumentSegment matching a concept query (§4).

    A pointer projection -- segment identity + lineage (material + node) +
    source_type + position anchor + concepts + size -- so the Mentor agent
    can answer "where in the course is concept X explained?" without
    dereferencing names one-by-one (the lineage fields are resolved in a
    single JOIN by :meth:`DocumentSegmentRepository.search_by_concepts`).

    The raw ``content`` is opt-in (``include_content``); the default hit is
    a pointer without segment text. ``secondary_concepts`` is populated
    only when ``include_secondary`` widened the query, else an empty list.
    """

    segment_id: uuid.UUID
    document_summary_id: uuid.UUID
    authored_document_id: uuid.UUID
    course_node_id: uuid.UUID
    node_title: str
    material_title: str
    filename: str | None
    source_type: str
    anchor: SegmentAnchor
    main_concepts: list[str]
    secondary_concepts: list[str]
    char_count: int | None
    content: str | None


def _resolve_anchor(source_type: str, row: Row[Any]) -> SegmentAnchor:
    """Build the SegmentAnchor for a result row from its ``source_type``.

    ``kind`` follows ``source_type`` (always defined); the matching bound
    scalars are then read off the projected row (the seven anchor scalars
    are always in the SELECT). For a text/web segment whose paragraph
    ordinal was never computed (KD-C), the pair is ``(None, None)`` but
    ``kind`` stays ``"paragraph"``.

    Every branch is EXPLICIT (task-code-materials ratification): a new
    source_type without a branch here fails loudly instead of silently
    mis-mapping into the paragraph kind.
    """
    if source_type in ("video", "audio"):
        return SegmentAnchor("time", row.start_time_sec, row.end_time_sec)
    if source_type == "presentation":
        return SegmentAnchor("slide", row.start_slide, row.end_slide)
    if source_type == "code":
        return SegmentAnchor("file", None, None, file_path=row.file_path)
    if source_type in ("text", "web"):
        # paragraph ordinals (may be None at the KD-C edge).
        return SegmentAnchor("paragraph", row.start_paragraph, row.end_paragraph)
    raise ValueError(f"no anchor mapping for source_type {source_type!r}")


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
                    is_auxiliary=draft.is_auxiliary,
                    # Video visual stream (task 2.4.6); [] for every other
                    # source type (draft.visual_content is None) -- coerced so
                    # the not-null JSONB column always receives a real array.
                    visual_content=[
                        ref.model_dump() for ref in (draft.visual_content or [])
                    ],
                    # Positional anchors (Phase 3.3a; 4th kind task-code-
                    # materials) — the source-type-specific position the
                    # pipeline computed on the draft, now persisted. Exactly
                    # one anchor is non-None per source_type (three numeric
                    # pairs + the single-column file_path); the rest stay None.
                    start_time_sec=draft.start_time_sec,
                    end_time_sec=draft.end_time_sec,
                    start_slide=draft.start_slide,
                    end_slide=draft.end_slide,
                    start_paragraph=draft.start_paragraph,
                    end_paragraph=draft.end_paragraph,
                    file_path=draft.file_path,
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

    async def search_by_concepts(
        self,
        *,
        course_root_id: uuid.UUID,
        concepts: list[str],
        include_secondary: bool = False,
        include_content: bool = False,
    ) -> list[ConceptSearchHit]:
        """Find segments teaching every concept in ``concepts`` (vision §4).

        Concept-navigation primitive for the Mentor agent: "where in the
        course is concept X explained?". Matches via JSONB ``@>``
        containment over the GIN-indexed concept columns (KD-gamma), so
        every term in ``concepts`` must be present in the segment (AND
        semantics; a single-term query is a list of one).

        The query JOINs up the lineage chain
        (``DocumentSegment`` -> ``DocumentSummary`` -> ``AuthoredDocument``
        -> ``CourseNode``) and projects names + source_type + anchor into
        each :class:`ConceptSearchHit`, so the caller never N+1-dereferences
        for display.

        Args:
            course_root_id: Denormalised root scope handle (KD-delta). The
                mandatory ``course_root_id = :root`` predicate prevents
                cross-course leakage.
            concepts: Concept terms, AND-combined via array containment.
                Bilingual-key aware -- a term matches the exact stored
                string with no query expansion (§4 amendment v0.20.10). An
                empty list returns ``[]`` (it does NOT scan the course).
            include_secondary: When ``True``, broaden the match to
                ``secondary_concepts`` as well (``main @> arr OR secondary
                @> arr``). Known limitation: a query split across columns
                (one term in main, another in secondary of the same
                segment) will not hit -- forward-note, acceptable for MVP.
                Also populates each hit's ``secondary_concepts``.
            include_content: When ``True``, add the raw segment ``content``
                column to the SELECT and project it; otherwise the heavy
                text column is never read from the DB and each hit's
                ``content`` is ``None``.

        Returns:
            Hits in course reading order: ``CourseNode.order`` (NULLS LAST)
            -> ``CourseNode.created_at`` -> ``AuthoredDocument.order`` ->
            ``DocumentSegment.order``. No relevance score (Phase 3.3b is
            position-ordered; relevance is the FD4/pgvector axis).
        """
        if not concepts:
            return []

        main_match = DocumentSegment.main_concepts.contains(concepts)
        match_predicate = (
            or_(main_match, DocumentSegment.secondary_concepts.contains(concepts))
            if include_secondary
            else main_match
        )

        # Narrow projection: exactly the columns ConceptSearchHit needs, never
        # whole entities. Whole-entity SELECT would pull the heavy
        # DocumentSegment.content / visual_content and the unused
        # material/node text columns on every hit -- wasted work on the hot
        # homework-review path. content is opt-in at the SELECT level so the
        # default (pointer) mode stays light on the wire, not just in the hit.
        stmt = select(
            DocumentSegment.id.label("segment_id"),
            DocumentSegment.document_summary_id.label("document_summary_id"),
            DocumentSummary.authored_document_id.label("authored_document_id"),
            AuthoredDocument.course_node_id.label("course_node_id"),
            CourseNode.title.label("node_title"),
            DocumentSummary.title.label("material_title"),
            AuthoredDocument.filename.label("filename"),
            AuthoredDocument.source_type.label("source_type"),
            DocumentSegment.main_concepts.label("main_concepts"),
            DocumentSegment.secondary_concepts.label("secondary_concepts"),
            DocumentSegment.content_char_count.label("char_count"),
            # Seven anchor scalars are always selected (cheap);
            # _resolve_anchor reads the one(s) matching the
            # source_type-derived kind.
            DocumentSegment.start_time_sec.label("start_time_sec"),
            DocumentSegment.end_time_sec.label("end_time_sec"),
            DocumentSegment.start_slide.label("start_slide"),
            DocumentSegment.end_slide.label("end_slide"),
            DocumentSegment.start_paragraph.label("start_paragraph"),
            DocumentSegment.end_paragraph.label("end_paragraph"),
            DocumentSegment.file_path.label("file_path"),
        )
        if include_content:
            stmt = stmt.add_columns(DocumentSegment.content.label("content"))
        stmt = (
            stmt.join(
                DocumentSummary,
                DocumentSegment.document_summary_id == DocumentSummary.id,
            )
            .join(
                AuthoredDocument,
                DocumentSummary.authored_document_id == AuthoredDocument.id,
            )
            .join(CourseNode, AuthoredDocument.course_node_id == CourseNode.id)
            .where(
                DocumentSegment.course_root_id == course_root_id,
                # Active segments only. A soft-deleted summary/material
                # cascade-soft-deletes its segments (CascadeDeleteService),
                # so this single predicate also excludes reprocess history
                # without joining deleted_at on every ancestor.
                DocumentSegment.deleted_at.is_(None),
                match_predicate,
            )
            .order_by(
                CourseNode.order.asc().nulls_last(),
                CourseNode.created_at.asc(),
                AuthoredDocument.order.asc(),
                DocumentSegment.order.asc(),
            )
        )

        result = await self._session.execute(stmt)
        return [
            ConceptSearchHit(
                segment_id=row.segment_id,
                document_summary_id=row.document_summary_id,
                authored_document_id=row.authored_document_id,
                course_node_id=row.course_node_id,
                node_title=row.node_title,
                material_title=row.material_title,
                filename=row.filename,
                source_type=row.source_type,
                anchor=_resolve_anchor(row.source_type, row),
                # JSONB columns deserialize straight to Python lists; the Row
                # projection yields a fresh per-row object (not a shared ORM
                # attribute), so no defensive copy is needed (reviewer 3.3b).
                main_concepts=row.main_concepts,
                secondary_concepts=(
                    row.secondary_concepts if include_secondary else []
                ),
                char_count=row.char_count,
                # content column is present only when include_content widened
                # the SELECT; guard the attribute access accordingly.
                content=row.content if include_content else None,
            )
            for row in result.all()
        ]
