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
dereference names one-by-one. Task 10 puts the hits in the course's
learning order (:func:`course_supporter.learning_order.in_learning_order`)
instead of comparing sibling numbers across the whole tree, and returns
only candidates -- segments a student may be pointed at
(:func:`_candidate_filters`). :meth:`DocumentSegmentRepository.list_candidates`
reads all of a course's candidates in the same order, for a reader that
matches concepts in Python (the course-link finder,
``homework/course_links.py``).

Standalone class (not a SoftDeleteRepository subclass) per the existing
repository convention (matches ``DocumentSummaryRepository`` shape).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import ColumnElement, Label, Row, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.ingestion.base import ProcessingError
from course_supporter.learning_order import MaterialPlace, TreeNode, in_learning_order
from course_supporter.material_visibility import (
    student_visible_roles,
    student_visible_states,
)
from course_supporter.models.source import SourceType
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
    a pointer without segment text. ``secondary_concepts`` is informational:
    the match reads ``main_concepts`` only (task 10, decision Б).
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


@dataclass(frozen=True, slots=True)
class CandidateSegment:
    """One candidate of a course, for a reader that matches in Python (task 10).

    A narrow projection of a segment a student may be pointed at
    (:func:`_candidate_filters`): identity, material, anchor, main concepts and
    the segment's learning-order place -- no segment text, no node title. The
    text of the few a reader picks is read apart
    (:meth:`DocumentSegmentRepository.read_contents`).
    """

    segment_id: uuid.UUID
    authored_document_id: uuid.UUID
    material_title: str
    source_type: str
    anchor: SegmentAnchor
    main_concepts: tuple[str, ...]
    place: MaterialPlace


@dataclass(frozen=True, slots=True)
class CourseCandidates:
    """Every candidate of a course, in learning order, and the tree that orders them.

    ``segments`` are in the order
    :func:`~course_supporter.learning_order.in_learning_order` gives over
    ``tree``. The tree is handed back so a reader places something else -- a
    task's document -- in the SAME order, without reading the course twice.
    """

    segments: tuple[CandidateSegment, ...]
    tree: tuple[TreeNode, ...]


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


def _candidate_filters(course_root_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """What makes a segment of the course a candidate (task 10, decision Б).

    A candidate is a segment a student may be pointed at: one from a document
    the portal shows them in the course tree right now. One definition for
    every reader of the course's candidates; the predicates name
    ``DocumentSummary`` and ``AuthoredDocument``, so the statement joins the
    segment's lineage. The concept match is the reader's own and is not here
    -- it reads ``main_concepts`` only, secondary concepts never count.

    - the segment is live and not a service file (``is_auxiliary``);
    - the summary is the document's active one, in ``ready``;
    - the document is live, and the portal's own rules show it: its role
      (:func:`~course_supporter.material_visibility.student_visible_roles`)
      and its processing state
      (:func:`~course_supporter.material_visibility.student_visible_states`
      over the SQL form of ``AuthoredDocument.state``) -- not copies of
      their conditions;
    - the document is not a task (any ``task_type``) and not a test the
      author wrote in the system (``test_object``).

    Each ``deleted_at`` is checked: the cascade normally keeps them in step,
    a candidate does not rely on it. ``is_auxiliary`` is the author's marking
    of a code file as auxiliary; recognising configs and dependencies
    (``pyproject.toml``…) at upload, which nobody marks by hand, is a separate
    ingestion task.
    """
    return (
        DocumentSegment.course_root_id == course_root_id,
        DocumentSegment.deleted_at.is_(None),
        DocumentSegment.is_auxiliary.is_(False),
        DocumentSummary.deleted_at.is_(None),
        DocumentSummary.status == "ready",
        AuthoredDocument.deleted_at.is_(None),
        AuthoredDocument.material_role.in_(student_visible_roles()),
        AuthoredDocument.state.in_(student_visible_states()),
        AuthoredDocument.task_type.is_(None),
        AuthoredDocument.source_type != SourceType.TEST_OBJECT.value,
    )


def _over_candidates(stmt: Select[Any], course_root_id: uuid.UUID) -> Select[Any]:
    """``stmt`` over the course's candidates: the lineage the filters name, joined.

    The one place the candidate statement is built, so every reader of the
    course's candidates gets the same rows; a reader adds its own predicate
    (the concept match) or joins (the node title) on top.
    """
    return (
        stmt.join(
            DocumentSummary,
            DocumentSegment.document_summary_id == DocumentSummary.id,
        )
        .join(
            AuthoredDocument,
            DocumentSummary.authored_document_id == AuthoredDocument.id,
        )
        .where(*_candidate_filters(course_root_id))
    )


def _anchor_columns() -> tuple[Label[Any], ...]:
    """The seven anchor scalars :func:`_resolve_anchor` reads (cheap, always)."""
    return (
        DocumentSegment.start_time_sec.label("start_time_sec"),
        DocumentSegment.end_time_sec.label("end_time_sec"),
        DocumentSegment.start_slide.label("start_slide"),
        DocumentSegment.end_slide.label("end_slide"),
        DocumentSegment.start_paragraph.label("start_paragraph"),
        DocumentSegment.end_paragraph.label("end_paragraph"),
        DocumentSegment.file_path.label("file_path"),
    )


def _place_columns() -> tuple[Label[Any], ...]:
    """The learning-order place :func:`_material_place` reads.

    It also reads ``course_node_id`` and ``authored_document_id``, which every
    reader projects for its own use.
    """
    return (
        AuthoredDocument.order.label("document_order"),
        AuthoredDocument.created_at.label("document_created_at"),
        DocumentSegment.order.label("segment_order"),
    )


def _material_place(row: Row[Any]) -> MaterialPlace:
    """The learning-order place of a result row (its node, document, segment)."""
    return MaterialPlace(
        course_node_id=row.course_node_id,
        document_order=row.document_order,
        document_created_at=row.document_created_at,
        document_id=row.authored_document_id,
        segment_order=row.segment_order,
    )


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
        include_content: bool = False,
    ) -> list[ConceptSearchHit]:
        """Find candidate segments teaching every concept in ``concepts`` (§4).

        Concept-navigation primitive for the Mentor agent: "where in the
        course is concept X explained?". Matches via JSONB ``@>``
        containment over the GIN-indexed ``main_concepts`` column
        (KD-gamma), so every term in ``concepts`` must be among the
        segment's main concepts (AND semantics; a single-term query is a
        list of one). Secondary concepts never match (task 10, decision Б),
        and only candidates are searched (:func:`_candidate_filters`).

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
            include_content: When ``True``, add the raw segment ``content``
                column to the SELECT and project it; otherwise the heavy
                text column is never read from the DB and each hit's
                ``content`` is ``None``.

        Returns:
            Hits in the course's learning order
            (:func:`~course_supporter.learning_order.in_learning_order`):
            nested nodes in sibling order, a node's own materials after
            everything nested in it, the root's materials last; inside a
            node, by ``AuthoredDocument.order`` then ``DocumentSegment.order``.
            No relevance score (position-ordered; relevance is the
            FD4/pgvector axis).
        """
        if not concepts:
            return []

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
            # _resolve_anchor reads the one(s) matching the
            # source_type-derived kind.
            *_anchor_columns(),
            *_place_columns(),
        )
        if include_content:
            stmt = stmt.add_columns(DocumentSegment.content.label("content"))
        stmt = (
            _over_candidates(stmt, course_root_id)
            .join(CourseNode, AuthoredDocument.course_node_id == CourseNode.id)
            .where(DocumentSegment.main_concepts.contains(concepts))
        )

        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return []
        rows = in_learning_order(
            rows, await self._course_tree(course_root_id), place=_material_place
        )
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
                secondary_concepts=row.secondary_concepts,
                char_count=row.char_count,
                # content column is present only when include_content widened
                # the SELECT; guard the attribute access accordingly.
                content=row.content if include_content else None,
            )
            for row in rows
        ]

    async def list_candidates(self, *, course_root_id: uuid.UUID) -> CourseCandidates:
        """Every candidate segment of the course, in learning order (task 10).

        For a reader that matches in Python what SQL containment cannot -- the
        course-link finder compares concepts by their normalisation key -- and
        so reads the course's candidates once and decides itself. The rows and
        their order are :meth:`search_by_concepts`'s, without the concept
        predicate: the same :func:`_over_candidates` statement, the same
        learning order over the same tree. The projection is narrow -- no
        segment text, no node title; the text of the segments a reader keeps
        comes from :meth:`read_contents`.

        Args:
            course_root_id: Denormalised root scope handle (KD-delta).

        Returns:
            The candidates and the course tree that orders them; ``segments``
            is empty for a course without candidates, the tree is still read.
        """
        stmt = _over_candidates(
            select(
                DocumentSegment.id.label("segment_id"),
                DocumentSummary.authored_document_id.label("authored_document_id"),
                AuthoredDocument.course_node_id.label("course_node_id"),
                DocumentSummary.title.label("material_title"),
                AuthoredDocument.source_type.label("source_type"),
                DocumentSegment.main_concepts.label("main_concepts"),
                *_anchor_columns(),
                *_place_columns(),
            ),
            course_root_id,
        )
        rows = (await self._session.execute(stmt)).all()
        tree = await self._course_tree(course_root_id)
        return CourseCandidates(
            segments=tuple(
                CandidateSegment(
                    segment_id=row.segment_id,
                    authored_document_id=row.authored_document_id,
                    material_title=row.material_title,
                    source_type=row.source_type,
                    anchor=_resolve_anchor(row.source_type, row),
                    main_concepts=tuple(row.main_concepts),
                    place=_material_place(row),
                )
                for row in in_learning_order(rows, tree, place=_material_place)
            ),
            tree=tuple(tree),
        )

    async def read_contents(
        self, segment_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """The text of each of ``segment_ids``, by id.

        A narrow read for the few segments a reader kept out of
        :meth:`list_candidates`: the heavy ``content`` column of exactly these
        rows. It filters nothing -- the ids come from a candidate read.
        """
        if not segment_ids:
            return {}
        rows = await self._session.execute(
            select(DocumentSegment.id, DocumentSegment.content).where(
                DocumentSegment.id.in_(segment_ids)
            )
        )
        return dict(rows.tuples().all())

    async def _course_tree(self, course_root_id: uuid.UUID) -> list[TreeNode]:
        """Every node of the course, as the learning order reads it.

        Soft-deleted nodes are walked too: the tree only places a hit, it never
        decides whether a segment is one -- that is the WHERE clause's job.
        """
        columns = (
            CourseNode.id,
            CourseNode.parent_id,
            CourseNode.order,
            CourseNode.created_at,
        )
        tree = (
            select(*columns)
            .where(CourseNode.id == course_root_id)
            .cte(name="course_tree", recursive=True)
        )
        tree = tree.union_all(
            select(*columns).join(tree, CourseNode.parent_id == tree.c.id)
        )
        result = await self._session.execute(select(tree))
        return [
            TreeNode(
                id=row.id,
                parent_id=row.parent_id,
                order=row.order,
                created_at=row.created_at,
            )
            for row in result.all()
        ]
