"""Unit tests for TextProcessor / WebProcessor.process_detail (Phase 2.1 C7).

Verifies Pass 2b algorithmic slicing behaviour for text/web materials.
Zero LLM calls -- the contract is purely deterministic offset arithmetic
over the canonical ``SourceDocument.assemble_text()`` reference string.

Both processors share an identical override (no audio/video-style
content cleaning here), so a single test module exercises both in
parallel classes.
"""

from __future__ import annotations

import pytest

from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


def _doc(text: str, source_type: SourceType = SourceType.TEXT) -> SourceDocument:
    """Build a SourceDocument whose assemble_text() == ``text`` (single chunk)."""
    chunk_type = (
        ChunkType.WEB_CONTENT if source_type == SourceType.WEB else ChunkType.PARAGRAPH
    )
    return SourceDocument(
        source_type=source_type,
        source_url="file:///fixture",
        chunks=[ContentChunk(chunk_type=chunk_type, text=text, index=0)],
    )


def _draft(
    *,
    order: int,
    start_pos: int,
    end_pos: int,
    title: str | None = None,
    description: str = "d",
    content: str | None = None,
) -> DocumentSegmentDraft:
    return DocumentSegmentDraft(
        order=order,
        start_pos=start_pos,
        end_pos=end_pos,
        title=title,
        description=description,
        main_concepts=[],
        secondary_concepts=[],
        content=content,
    )


def _summary(segments: list[DocumentSegmentDraft]) -> DocumentSummaryDraft:
    # Per fixup 2.1.7.1 invariants: content_char_count must equal last
    # segment end_pos when segments non-empty.
    content_char_count = segments[-1].end_pos if segments else 0
    return DocumentSummaryDraft(
        title="t",
        description="d",
        main_concepts=[],
        secondary_concepts=[],
        content_char_count=content_char_count,
        segments=segments,
    )


class TestTextProcessorProcessDetail:
    """``TextProcessor.process_detail`` algorithmic slice (KD-2.1-O)."""

    @pytest.mark.asyncio
    async def test_empty_segments_returns_empty_list(self) -> None:
        processor = TextProcessor()
        doc = _doc("anything")
        result = await processor.process_detail(doc, _summary([]))
        assert result == []

    @pytest.mark.asyncio
    async def test_single_segment_slice(self) -> None:
        processor = TextProcessor()
        text = "Decorators in Python wrap functions."
        doc = _doc(text)
        drafts = [_draft(order=0, start_pos=0, end_pos=len(text))]

        result = await processor.process_detail(doc, _summary(drafts))

        assert len(result) == 1
        assert result[0].content == text
        assert result[0].order == 0

    @pytest.mark.asyncio
    async def test_multi_segment_slice_in_order(self) -> None:
        """Contiguous segment cover per fixup 2.1.7.1 invariants."""
        processor = TextProcessor()
        text = "Lexing stage.\n\nParsing stage.\n\nAST stage."
        doc = _doc(text)
        drafts = [
            _draft(order=0, start_pos=0, end_pos=15),
            _draft(order=1, start_pos=15, end_pos=31),
            _draft(order=2, start_pos=31, end_pos=len(text)),
        ]

        result = await processor.process_detail(doc, _summary(drafts))

        assert [s.content for s in result] == [
            "Lexing stage.\n\n",
            "Parsing stage.\n\n",
            "AST stage.",
        ]
        assert [s.order for s in result] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_draft_with_non_none_content_passes_through(self) -> None:
        """Defensive: pre-filled content is not re-sliced."""
        processor = TextProcessor()
        doc = _doc("would slice this text otherwise")
        drafts = [
            _draft(order=0, start_pos=0, end_pos=5, content="kept verbatim"),
        ]

        result = await processor.process_detail(doc, _summary(drafts))

        assert result[0].content == "kept verbatim"

    @pytest.mark.asyncio
    async def test_does_not_mutate_input_draft(self) -> None:
        """Caller-provided drafts must remain unchanged (model_copy used)."""
        processor = TextProcessor()
        text = "first body.\n\nsecond body."
        doc = _doc(text)
        original = _draft(order=0, start_pos=0, end_pos=11)
        drafts = [original]

        result = await processor.process_detail(doc, _summary(drafts))

        assert original.content is None
        assert result[0].content == "first body."

    @pytest.mark.asyncio
    async def test_multi_chunk_assembly_offsets(self) -> None:
        """Offsets land on the assembled string, not individual chunks."""
        processor = TextProcessor()
        chunks = [
            ContentChunk(chunk_type=ChunkType.HEADING, text="Header", index=0),
            ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="Body.", index=1),
        ]
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///fixture",
            chunks=chunks,
        )
        # assemble_text() => "Header\n\nBody." (len 13). Contiguous cover
        # per fixup 2.1.7.1 — separator chars are absorbed into the
        # preceding segment for fixture purposes.
        drafts = [
            _draft(order=0, start_pos=0, end_pos=8),
            _draft(order=1, start_pos=8, end_pos=13),
        ]

        result = await processor.process_detail(doc, _summary(drafts))

        assert result[0].content == "Header\n\n"
        assert result[1].content == "Body."


class TestWebProcessorProcessDetail:
    """``WebProcessor.process_detail`` mirrors TextProcessor (KD-2.1-O)."""

    @pytest.mark.asyncio
    async def test_single_segment_slice(self) -> None:
        processor = WebProcessor()
        text = "Article body content here."
        doc = _doc(text, source_type=SourceType.WEB)
        drafts = [_draft(order=0, start_pos=0, end_pos=len(text))]

        result = await processor.process_detail(doc, _summary(drafts))

        assert result[0].content == text

    @pytest.mark.asyncio
    async def test_multi_segment_slice(self) -> None:
        """Contiguous segment cover per fixup 2.1.7.1 invariants."""
        processor = WebProcessor()
        text = "intro para.\n\nsecond para.\n\nthird para."
        doc = _doc(text, source_type=SourceType.WEB)
        drafts = [
            _draft(order=0, start_pos=0, end_pos=13),
            _draft(order=1, start_pos=13, end_pos=27),
            _draft(order=2, start_pos=27, end_pos=len(text)),
        ]

        result = await processor.process_detail(doc, _summary(drafts))

        assert [s.content for s in result] == [
            "intro para.\n\n",
            "second para.\n\n",
            "third para.",
        ]

    @pytest.mark.asyncio
    async def test_empty_segments_returns_empty_list(self) -> None:
        processor = WebProcessor()
        doc = _doc("any", source_type=SourceType.WEB)
        result = await processor.process_detail(doc, _summary([]))
        assert result == []
