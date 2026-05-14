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

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSegmentDraft
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.storage.document_segment_repository import (
    DocumentSegmentRepository,
)
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
)

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
