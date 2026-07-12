"""Integration tests for DocumentSegmentRepository (Phase 2.1 C7).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db -v`` against this file.

Covers Phase 2.1 C7 (KD-2.1-O + KD-2.1-F):

* ``create_batch`` materialises N DocumentSegment rows in input order,
  inherits ``course_root_id`` from the parent DocumentSummary, and
  slices ``content`` from ``source_doc.assemble_text()`` for drafts that
  do not pre-fill it.
* Drafts that already carry ``content`` are passed through verbatim
  (defensive path for future audio/video processors that fill in
  ``process_detail``).
* Title + description from the draft are persisted (alpha-minimal
  ratify, 2026-05-12).
* ``create_batch`` invokes ``ContentHashService.invalidate_up`` per
  segment so the parent ``DocumentSummary`` + ``AuthoredDocument`` +
  root ``CourseNode`` all receive fresh ``content_hash`` values
  (KD-2.1-F).
* Out-of-bounds offsets raise ``ProcessingError`` rather than silently
  emitting wrong segments.
* Unknown ``document_summary_id`` raises ``ValueError``.
* Empty draft list short-circuits without DB writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSegmentDraft, VisualSceneRef
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.storage.cascade import (
    CascadeDeleteService,
    build_cascade_map,
)
from course_supporter.storage.document_segment_repository import (
    ConceptSearchHit,
    DocumentSegmentRepository,
    SegmentAnchor,
)
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


def _source_doc(text: str) -> SourceDocument:
    """Construct a SourceDocument whose assemble_text() == ``text``.

    Single chunk avoids the ``"\\n\\n".join`` boundary mattering -- the
    repository slicing uses ``assemble_text()`` so the reference text
    is exactly ``text``.
    """
    return SourceDocument(
        source_type=SourceType.WEB,
        source_url="https://example.com/integ",
        chunks=[ContentChunk(chunk_type=ChunkType.WEB_CONTENT, text=text, index=0)],
    )


def _draft(
    *,
    order: int,
    start_pos: int,
    end_pos: int,
    title: str | None = None,
    description: str = "seg desc",
    main: list[str] | None = None,
    secondary: list[str] | None = None,
    content: str | None = None,
    visual: list[VisualSceneRef] | None = None,
    start_time_sec: float | None = None,
    end_time_sec: float | None = None,
    start_slide: int | None = None,
    end_slide: int | None = None,
) -> DocumentSegmentDraft:
    return DocumentSegmentDraft(
        order=order,
        start_pos=start_pos,
        end_pos=end_pos,
        title=title,
        description=description,
        main_concepts=main or [],
        secondary_concepts=secondary or [],
        content=content,
        visual_content=visual,
        start_time_sec=start_time_sec,
        end_time_sec=end_time_sec,
        start_slide=start_slide,
        end_slide=end_slide,
    )


async def _make_summary(
    db_session: AsyncSession, entry: AuthoredDocument
) -> DocumentSummary:
    repo = DocumentSummaryRepository(db_session)
    return await repo.create(
        authored_document_id=entry.id,
        title="Parent Summary",
        description="d",
        main_concepts=[],
        secondary_concepts=[],
        content_char_count=100,
    )


class TestCreateBatch:
    """``create_batch`` materialisation + slicing + cascade discipline."""

    async def test_persists_n_segments_with_sliced_content(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        summary = await _make_summary(db_session, seed_material_entry)
        text = "alpha block.\n\nbeta block.\n\ngamma block."
        doc = _source_doc(text)
        # Contiguous cover per fixup 2.1.7.1 — separators absorbed into
        # preceding segment for fixture purposes.
        drafts = [
            _draft(order=0, start_pos=0, end_pos=14, title="A"),
            _draft(order=1, start_pos=14, end_pos=27, title="B"),
            _draft(order=2, start_pos=27, end_pos=len(text), title="C"),
        ]

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, drafts, source_doc=doc)

        assert [s.order for s in segments] == [0, 1, 2]
        assert segments[0].content == "alpha block.\n\n"
        assert segments[1].content == "beta block.\n\n"
        assert segments[2].content == "gamma block."
        for seg in segments:
            assert seg.document_summary_id == summary.id
            assert seg.course_root_id == summary.course_root_id
            assert seg.content_char_count == len(seg.content)
            assert seg.content_hash is not None

    async def test_persists_title_and_description_from_draft(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Alpha-minimal ratify: title + description columns hold draft values."""
        summary = await _make_summary(db_session, seed_material_entry)
        text = "lorem ipsum dolor sit amet"
        doc = _source_doc(text)
        drafts = [
            _draft(
                order=0,
                start_pos=0,
                end_pos=len(text),
                title="Lorem Section",
                description="Walks through lorem ipsum framing.",
            )
        ]

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, drafts, source_doc=doc)

        assert segments[0].title == "Lorem Section"
        assert segments[0].description == "Walks through lorem ipsum framing."

    async def test_persists_positional_anchors_from_draft(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Phase 3.3a: time/slide anchors computed upstream are persisted
        (previously dropped on the draft→ORM boundary). One pair per row;
        the other anchor columns stay NULL.
        """
        summary = await _make_summary(db_session, seed_material_entry)
        text = "alpha block.\n\nbeta block."
        doc = _source_doc(text)
        drafts = [
            # Audio/video-shaped draft — time anchors set, slide/paragraph NULL.
            _draft(
                order=0,
                start_pos=0,
                end_pos=14,
                content="alpha block.\n\n",
                start_time_sec=12.5,
                end_time_sec=47.0,
            ),
            # Presentation-shaped draft — slide anchors set, time/paragraph NULL.
            _draft(
                order=1,
                start_pos=14,
                end_pos=len(text),
                content="beta block.",
                start_slide=3,
                end_slide=5,
            ),
        ]

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, drafts, source_doc=doc)

        time_seg, slide_seg = segments[0], segments[1]
        assert time_seg.start_time_sec == 12.5
        assert time_seg.end_time_sec == 47.0
        assert time_seg.start_slide is None
        assert time_seg.end_slide is None
        assert time_seg.start_paragraph is None  # not yet computed (commit-2)

        assert slide_seg.start_slide == 3
        assert slide_seg.end_slide == 5
        assert slide_seg.start_time_sec is None
        assert slide_seg.end_time_sec is None

    async def test_draft_with_non_none_content_passes_through(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Defensive path: drafts that pre-fill content are not re-sliced."""
        summary = await _make_summary(db_session, seed_material_entry)
        text = "reference text not used because draft.content is set"
        doc = _source_doc(text)
        drafts = [
            _draft(
                order=0,
                start_pos=0,
                end_pos=10,
                content="pre-filled",
            )
        ]

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, drafts, source_doc=doc)

        assert segments[0].content == "pre-filled"
        assert segments[0].content_char_count == len("pre-filled")

    async def test_persists_visual_content_round_trip(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Task 2.4.6: a video draft's visual stream round-trips through the
        JSONB column as ordered dicts; a draft without it persists ``[]``
        (not-null server_default)."""
        summary = await _make_summary(db_session, seed_material_entry)
        text = "video transcript alpha beta"
        doc = _source_doc(text)
        drafts = [
            _draft(
                order=0,
                start_pos=0,
                end_pos=10,
                content="alpha",
                visual=[
                    VisualSceneRef(position_ms=0, description="slide A", kind="anchor"),
                    VisualSceneRef(position_ms=9000, description="slide B", scene_id=1),
                ],
            ),
            _draft(order=1, start_pos=10, end_pos=len(text), content="beta"),
        ]

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, drafts, source_doc=doc)

        assert segments[0].visual_content == [
            {
                "position_ms": 0,
                "description": "slide A",
                "kind": "anchor",
                "scene_id": 0,
            },
            {"position_ms": 9000, "description": "slide B", "kind": "", "scene_id": 1},
        ]
        # Non-video / visual-less draft -> empty array, never NULL.
        assert segments[1].visual_content == []

        # Re-read from a fresh query to confirm JSONB persistence (not just
        # the in-session instance).
        reloaded = (
            await db_session.execute(
                select(DocumentSegment).where(DocumentSegment.id == segments[0].id)
            )
        ).scalar_one()
        assert reloaded.visual_content[0]["description"] == "slide A"

    async def test_cascade_propagates_content_hash_to_root(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """KD-2.1-F: per-segment invalidate_up reaches root CourseNode."""
        summary = await _make_summary(db_session, seed_material_entry)
        pre_summary_hash = summary.content_hash
        await db_session.refresh(seed_material_entry)
        await db_session.refresh(seed_root_node)
        pre_entry_hash = seed_material_entry.content_hash
        pre_root_hash = seed_root_node.content_hash

        text = "block one body here.\n\nblock two body here."
        doc = _source_doc(text)
        drafts = [
            _draft(order=0, start_pos=0, end_pos=22),
            _draft(order=1, start_pos=22, end_pos=len(text)),
        ]

        repo = DocumentSegmentRepository(db_session)
        await repo.create_batch(summary.id, drafts, source_doc=doc)

        await db_session.refresh(summary)
        await db_session.refresh(seed_material_entry)
        await db_session.refresh(seed_root_node)

        assert summary.content_hash is not None
        assert seed_material_entry.content_hash is not None
        assert seed_root_node.content_hash is not None
        assert summary.content_hash != pre_summary_hash
        assert seed_material_entry.content_hash != pre_entry_hash
        assert seed_root_node.content_hash != pre_root_hash

    async def test_segments_visible_via_select(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Segments are queryable post-flush by summary FK."""
        summary = await _make_summary(db_session, seed_material_entry)
        text = "first.\n\nsecond."
        doc = _source_doc(text)
        drafts = [
            _draft(order=0, start_pos=0, end_pos=8),
            _draft(order=1, start_pos=8, end_pos=len(text)),
        ]

        repo = DocumentSegmentRepository(db_session)
        await repo.create_batch(summary.id, drafts, source_doc=doc)

        result = await db_session.execute(
            select(DocumentSegment)
            .where(DocumentSegment.document_summary_id == summary.id)
            .order_by(DocumentSegment.order)
        )
        rows = list(result.scalars().all())
        assert len(rows) == 2
        # Separator absorbed into preceding segment per contiguous cover.
        assert rows[0].content == "first.\n\n"
        assert rows[1].content == "second."

    async def test_empty_draft_list_short_circuits(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        summary = await _make_summary(db_session, seed_material_entry)
        doc = _source_doc("anything")

        repo = DocumentSegmentRepository(db_session)
        segments = await repo.create_batch(summary.id, [], source_doc=doc)

        assert segments == []
        result = await db_session.execute(
            select(DocumentSegment).where(
                DocumentSegment.document_summary_id == summary.id
            )
        )
        assert list(result.scalars().all()) == []


class TestCreateBatchErrors:
    """Boundary + missing-parent error paths.

    Note: per-segment offset invariants (``start_pos >= 0``,
    ``end_pos > start_pos``) are now enforced at the Pydantic schema
    level — see ``test_ingestion_schemas.py``
    ``TestDocumentSegmentDraftOffsetInvariants``. The repository's
    bounds-check is retained as defence-in-depth (model_construct
    bypass safety) but the unique error path it owns is ``end_pos >
    reference_text length``.
    """

    async def test_out_of_bounds_end_pos_raises(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """end_pos > len(reference_text) — only repo-owned error path."""
        summary = await _make_summary(db_session, seed_material_entry)
        text = "short"
        doc = _source_doc(text)
        drafts = [_draft(order=0, start_pos=0, end_pos=99)]

        repo = DocumentSegmentRepository(db_session)
        with pytest.raises(ProcessingError, match="out of bounds"):
            await repo.create_batch(summary.id, drafts, source_doc=doc)

    async def test_unknown_summary_id_raises(
        self,
        db_session: AsyncSession,
    ) -> None:
        repo = DocumentSegmentRepository(db_session)
        doc = _source_doc("anything")
        ghost = uuid.uuid4()

        with pytest.raises(ValueError, match="DocumentSummary not found"):
            await repo.create_batch(
                ghost,
                [_draft(order=0, start_pos=0, end_pos=5)],
                source_doc=doc,
            )


# ── search_by_concepts seed helpers (Phase 3.3b) ──────────────────


async def _seed_root(
    session: AsyncSession,
    tenant: Tenant,
    *,
    title: str = "Course",
    order: int | None = 0,
) -> CourseNode:
    """Create a root CourseNode under ``tenant`` (language defaulted)."""
    node = make_root_course_node(tenant_id=tenant.id, title=title, order=order)
    session.add(node)
    await session.flush()
    return node


async def _seed_child(
    session: AsyncSession,
    tenant: Tenant,
    parent: CourseNode,
    *,
    title: str = "Section",
    order: int | None = 0,
) -> CourseNode:
    """Create a child CourseNode (raw — child needs no default_language)."""
    node = CourseNode(
        tenant_id=tenant.id,
        parent_id=parent.id,
        title=title,
        order=order,
    )
    session.add(node)
    await session.flush()
    return node


async def _seed_material(
    session: AsyncSession,
    node: CourseNode,
    root: CourseNode,
    *,
    source_type: str = "text",
    order: int = 0,
    filename: str | None = "material.txt",
) -> AuthoredDocument:
    """Create an AuthoredDocument on ``node`` rooted at ``root``."""
    doc = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=root.id,
        source_type=source_type,
        source_url="https://example.com/search",
        order=order,
        filename=filename,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _seed_summary(
    session: AsyncSession,
    doc: AuthoredDocument,
    root: CourseNode,
    *,
    title: str = "Material Title",
) -> DocumentSummary:
    """Create the active DocumentSummary for ``doc``."""
    summary = DocumentSummary(
        authored_document_id=doc.id,
        course_root_id=root.id,
        title=title,
        status="ready",
    )
    session.add(summary)
    await session.flush()
    return summary


async def _seed_segment(
    session: AsyncSession,
    summary: DocumentSummary,
    root: CourseNode,
    *,
    order: int = 0,
    main: list[str] | None = None,
    secondary: list[str] | None = None,
    content: str = "segment body",
    deleted: bool = False,
    start_time_sec: float | None = None,
    end_time_sec: float | None = None,
    start_slide: int | None = None,
    end_slide: int | None = None,
    start_paragraph: int | None = None,
    end_paragraph: int | None = None,
    file_path: str | None = None,
) -> DocumentSegment:
    """Create one DocumentSegment with explicit concepts + anchors."""
    seg = DocumentSegment(
        document_summary_id=summary.id,
        course_root_id=root.id,
        order=order,
        start_pos=0,
        end_pos=max(len(content), 1),
        content=content,
        content_char_count=len(content),
        main_concepts=main or [],
        secondary_concepts=secondary or [],
        start_time_sec=start_time_sec,
        end_time_sec=end_time_sec,
        start_slide=start_slide,
        end_slide=end_slide,
        start_paragraph=start_paragraph,
        end_paragraph=end_paragraph,
        file_path=file_path,
    )
    if deleted:
        seg.deleted_at = datetime.now(UTC)
    session.add(seg)
    await session.flush()
    return seg


async def _one_segment_course(
    session: AsyncSession,
    tenant: Tenant,
    *,
    main: list[str],
    secondary: list[str] | None = None,
    content: str = "segment body",
    source_type: str = "text",
    deleted: bool = False,
    start_paragraph: int | None = None,
    end_paragraph: int | None = None,
) -> tuple[CourseNode, DocumentSegment]:
    """Build root → material → summary → one segment; return (root, segment)."""
    root = await _seed_root(session, tenant)
    doc = await _seed_material(session, root, root, source_type=source_type)
    summary = await _seed_summary(session, doc, root)
    seg = await _seed_segment(
        session,
        summary,
        root,
        main=main,
        secondary=secondary,
        content=content,
        deleted=deleted,
        start_paragraph=start_paragraph,
        end_paragraph=end_paragraph,
    )
    return root, seg


class TestSearchByConcepts:
    """``search_by_concepts`` JSONB ``@>`` navigation (Phase 3.3b §4)."""

    async def test_single_term_main_hit_with_lineage(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A single-term query hits a main-concept segment; lineage filled."""
        root, seg = await _one_segment_course(
            db_session, seed_tenant, main=["recursion"]
        )

        repo = DocumentSegmentRepository(db_session)
        hits = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["recursion"]
        )

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, ConceptSearchHit)
        assert hit.segment_id == seg.id
        assert hit.node_title == "Course"
        assert hit.material_title == "Material Title"
        assert hit.filename == "material.txt"
        assert hit.source_type == "text"
        assert hit.char_count == len("segment body")
        assert hit.main_concepts == ["recursion"]
        # pointer default: no raw text, no secondary projection.
        assert hit.content is None
        assert hit.secondary_concepts == []

    async def test_and_multi_term_requires_all(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """AND semantics: every term must be present in the same segment."""
        root, seg = await _one_segment_course(
            db_session, seed_tenant, main=["closures", "scope"]
        )

        repo = DocumentSegmentRepository(db_session)
        # Both terms present → hit.
        both = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["closures", "scope"]
        )
        assert [h.segment_id for h in both] == [seg.id]
        # One term absent → no hit (containment of the whole array fails).
        missing = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["closures", "generators"]
        )
        assert missing == []

    async def test_include_secondary_broadens_and_projects(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """include_secondary widens the match and fills secondary_concepts."""
        root, seg = await _one_segment_course(
            db_session, seed_tenant, main=["loops"], secondary=["iterables"]
        )

        repo = DocumentSegmentRepository(db_session)
        # Secondary-only term is invisible by default.
        narrow = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["iterables"]
        )
        assert narrow == []
        # With include_secondary it matches and projects secondary_concepts.
        wide = await repo.search_by_concepts(
            course_root_id=root.id,
            concepts=["iterables"],
            include_secondary=True,
        )
        assert [h.segment_id for h in wide] == [seg.id]
        assert wide[0].secondary_concepts == ["iterables"]

    async def test_include_content_projection(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """content is None by default, raw text only when include_content.

        Behavioural control of the SELECT composition: with the narrow
        projection, include_content=False omits the ``content`` column
        entirely (heavy text never leaves the DB) and the hit's ``content``
        is None; include_content=True adds the column and projects it. We
        assert the observable behaviour, not the generated SQL text.
        """
        root, _ = await _one_segment_course(
            db_session, seed_tenant, main=["decorators"], content="full body text"
        )

        repo = DocumentSegmentRepository(db_session)
        without = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["decorators"]
        )
        assert without[0].content is None
        with_content = await repo.search_by_concepts(
            course_root_id=root.id,
            concepts=["decorators"],
            include_content=True,
        )
        assert with_content[0].content == "full body text"

    async def test_scope_isolation_excludes_other_course(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A segment in another course_root is never returned."""
        root_a, seg_a = await _one_segment_course(
            db_session, seed_tenant, main=["shared"]
        )
        _root_b, seg_b = await _one_segment_course(
            db_session, seed_tenant, main=["shared"]
        )

        repo = DocumentSegmentRepository(db_session)
        hits = await repo.search_by_concepts(
            course_root_id=root_a.id, concepts=["shared"]
        )
        assert [h.segment_id for h in hits] == [seg_a.id]
        assert seg_b.id not in {h.segment_id for h in hits}

    async def test_soft_deleted_segment_excluded(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A soft-deleted segment (reprocess history) is not a hit."""
        root, _ = await _one_segment_course(
            db_session, seed_tenant, main=["staleconcept"], deleted=True
        )

        repo = DocumentSegmentRepository(db_session)
        hits = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["staleconcept"]
        )
        assert hits == []

    async def test_ancestor_cascade_soft_delete_excludes_segment(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Soft-deleting the parent DocumentSummary drops its segment.

        ``CascadeDeleteService`` propagates ``deleted_at`` from the summary
        into its segments, so the (formerly active) segment falls out of
        results -- the end-to-end form of the in-code comment's reliance on
        cascade, not just a direct segment soft-delete.
        """
        root = await _seed_root(db_session, seed_tenant)
        doc = await _seed_material(db_session, root, root)
        summary = await _seed_summary(db_session, doc, root)
        seg = await _seed_segment(db_session, summary, root, main=["cascaded"])

        repo = DocumentSegmentRepository(db_session)
        before = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["cascaded"]
        )
        assert [h.segment_id for h in before] == [seg.id]

        await CascadeDeleteService(db_session).soft_delete_with_cascade(
            summary, build_cascade_map(DocumentSummary)
        )

        after = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["cascaded"]
        )
        assert after == []

    async def test_bilingual_key_hits_same_row(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A bilingual concept pair: local + canon form hit the SAME segment.

        No query expansion -- the term matches the exact stored string,
        so both ``"змінна"`` and ``"variable"`` land on the one segment
        that carries the pair (§4 amendment v0.20.10).
        """
        root, seg = await _one_segment_course(
            db_session, seed_tenant, main=["змінна", "variable"]
        )

        repo = DocumentSegmentRepository(db_session)
        local = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["змінна"]
        )
        canon = await repo.search_by_concepts(
            course_root_id=root.id, concepts=["variable"]
        )
        assert [h.segment_id for h in local] == [seg.id]
        assert [h.segment_id for h in canon] == [seg.id]

    async def test_empty_concepts_returns_empty(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """An empty concept list short-circuits to [] (no full scan)."""
        root, _ = await _one_segment_course(db_session, seed_tenant, main=["anything"])

        repo = DocumentSegmentRepository(db_session)
        assert await repo.search_by_concepts(course_root_id=root.id, concepts=[]) == []

    async def test_anchor_kind_from_source_type(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Anchor kind follows source_type; bounds read off matching columns."""
        # video → time anchor.
        root_v = await _seed_root(db_session, seed_tenant, title="V")
        doc_v = await _seed_material(db_session, root_v, root_v, source_type="video")
        sum_v = await _seed_summary(db_session, doc_v, root_v)
        await _seed_segment(
            db_session,
            sum_v,
            root_v,
            main=["nav"],
            start_time_sec=12.5,
            end_time_sec=47.0,
        )
        # presentation → slide anchor.
        root_p = await _seed_root(db_session, seed_tenant, title="P")
        doc_p = await _seed_material(
            db_session, root_p, root_p, source_type="presentation"
        )
        sum_p = await _seed_summary(db_session, doc_p, root_p)
        await _seed_segment(
            db_session, sum_p, root_p, main=["nav"], start_slide=3, end_slide=5
        )
        # text with paragraph anchor.
        root_t = await _seed_root(db_session, seed_tenant, title="T")
        doc_t = await _seed_material(db_session, root_t, root_t, source_type="text")
        sum_t = await _seed_summary(db_session, doc_t, root_t)
        await _seed_segment(
            db_session,
            sum_t,
            root_t,
            main=["nav"],
            start_paragraph=2,
            end_paragraph=4,
        )

        repo = DocumentSegmentRepository(db_session)
        (vhit,) = await repo.search_by_concepts(
            course_root_id=root_v.id, concepts=["nav"]
        )
        (phit,) = await repo.search_by_concepts(
            course_root_id=root_p.id, concepts=["nav"]
        )
        (thit,) = await repo.search_by_concepts(
            course_root_id=root_t.id, concepts=["nav"]
        )

        assert vhit.anchor == SegmentAnchor("time", 12.5, 47.0)
        assert phit.anchor == SegmentAnchor("slide", 3, 5)
        assert thit.anchor == SegmentAnchor("paragraph", 2, 4)

    async def test_code_anchor_kind_file_path(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Code segment → ('file', None, None, file_path=...) anchor.

        task-code-materials ratify: the fourth anchor kind is the single
        ``file_path`` column; the numeric bounds stay None and exactly one
        anchor is non-null on the row.
        """
        root = await _seed_root(db_session, seed_tenant, title="C")
        doc = await _seed_material(db_session, root, root, source_type="code")
        summary = await _seed_summary(db_session, doc, root)
        seg = await _seed_segment(
            db_session,
            summary,
            root,
            main=["nav"],
            file_path="src/app.py",
        )
        # Exactly-one-anchor invariant at the ORM row level.
        assert seg.file_path == "src/app.py"
        assert seg.start_time_sec is None and seg.end_time_sec is None
        assert seg.start_slide is None and seg.end_slide is None
        assert seg.start_paragraph is None and seg.end_paragraph is None

        repo = DocumentSegmentRepository(db_session)
        (hit,) = await repo.search_by_concepts(course_root_id=root.id, concepts=["nav"])
        assert hit.anchor == SegmentAnchor("file", None, None, file_path="src/app.py")

    def test_unknown_source_type_fails_loud(self) -> None:
        """Bare paragraph fallback is forbidden (task-code-materials ratify).

        A future source_type without an explicit _resolve_anchor branch
        must raise, never silently mis-map into the paragraph kind.
        """
        from course_supporter.storage.document_segment_repository import (
            _resolve_anchor,
        )

        with pytest.raises(ValueError, match="no anchor mapping"):
            _resolve_anchor("hologram", MagicMock())

    async def test_paragraph_edge_anchor_kind_kept(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Text segment with no computed paragraph → ('paragraph', None, None).

        kind still resolves from source_type, so the consumer knows it is a
        text material even with no exact bounds (KD-C / DD-3.3a-A).
        """
        root, _ = await _one_segment_course(
            db_session,
            seed_tenant,
            main=["nav"],
            source_type="web",
            start_paragraph=None,
            end_paragraph=None,
        )

        repo = DocumentSegmentRepository(db_session)
        (hit,) = await repo.search_by_concepts(course_root_id=root.id, concepts=["nav"])
        assert hit.anchor == SegmentAnchor("paragraph", None, None)

    async def test_results_in_course_reading_order(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Order = CourseNode.order (NULLS LAST), then doc.order, seg.order."""
        root = await _seed_root(db_session, seed_tenant, title="Root", order=0)
        # Child node with NULL order → sorts after the root node.
        child = await _seed_child(
            db_session, seed_tenant, root, title="Late", order=None
        )

        # Root node, material order=0: two segments inserted out of order.
        doc_a = await _seed_material(db_session, root, root, order=0, filename="a")
        sum_a = await _seed_summary(db_session, doc_a, root, title="A")
        seg_a1 = await _seed_segment(db_session, sum_a, root, order=1, main=["nav"])
        seg_a0 = await _seed_segment(db_session, sum_a, root, order=0, main=["nav"])
        # Root node, material order=1.
        doc_b = await _seed_material(db_session, root, root, order=1, filename="b")
        sum_b = await _seed_summary(db_session, doc_b, root, title="B")
        seg_b = await _seed_segment(db_session, sum_b, root, order=0, main=["nav"])
        # Child node (NULL order) → last.
        doc_c = await _seed_material(db_session, child, root, order=0, filename="c")
        sum_c = await _seed_summary(db_session, doc_c, root, title="C")
        seg_c = await _seed_segment(db_session, sum_c, root, order=0, main=["nav"])

        repo = DocumentSegmentRepository(db_session)
        hits = await repo.search_by_concepts(course_root_id=root.id, concepts=["nav"])

        assert [h.segment_id for h in hits] == [
            seg_a0.id,  # node order 0, doc order 0, seg order 0
            seg_a1.id,  # node order 0, doc order 0, seg order 1
            seg_b.id,  # node order 0, doc order 1
            seg_c.id,  # node order NULL → last
        ]
