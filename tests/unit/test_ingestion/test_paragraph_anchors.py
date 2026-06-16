"""Unit tests for the Phase 3.3a paragraph-anchor bridge."""

from __future__ import annotations

from course_supporter.ingestion.paragraph_anchors import (
    chars_per_paragraph_cumsum,
    compute_paragraph_anchors,
)
from course_supporter.models.source import ChunkType, ContentChunk

_TEXT = frozenset({ChunkType.PARAGRAPH})
_WEB = frozenset({ChunkType.WEB_CONTENT})


def _chunk(kind: ChunkType, text: str, index: int) -> ContentChunk:
    return ContentChunk(chunk_type=kind, text=text, index=index)


# "alpha\n\nTitle\n\nbeta\n\ngamma" — chunk offsets:
# alpha[0,7) Title[7,14) beta[14,20) gamma[20,25)
def _text_doc_chunks() -> list[ContentChunk]:
    return [
        _chunk(ChunkType.PARAGRAPH, "alpha", 0),
        _chunk(ChunkType.HEADING, "Title", 1),
        _chunk(ChunkType.PARAGRAPH, "beta", 2),
        _chunk(ChunkType.PARAGRAPH, "gamma", 3),
    ]


class TestCumsum:
    def test_empty_is_single_zero(self) -> None:
        assert chars_per_paragraph_cumsum([]) == [0]

    def test_offsets_match_assemble_text_with_separators(self) -> None:
        cumsum = chars_per_paragraph_cumsum(_text_doc_chunks())
        # "\n\n" (2) between non-last chunks; last chunk owns no separator.
        assert cumsum == [0, 7, 14, 20, 25]
        # Invariant: final offset == len(assemble_text).
        assert cumsum[-1] == len("alpha\n\nTitle\n\nbeta\n\ngamma")


class TestComputeParagraphAnchors:
    def test_headings_do_not_advance_the_ordinal(self) -> None:
        chunks = _text_doc_chunks()
        # Segment over alpha + the Title heading → only paragraph is alpha=0.
        assert compute_paragraph_anchors(chunks, _TEXT, 0, 14) == (0, 0)
        # Segment over beta + gamma → paragraphs 1 and 2 (Title not counted).
        assert compute_paragraph_anchors(chunks, _TEXT, 14, 25) == (1, 2)

    def test_boundary_in_heading_falls_back_to_nearest_paragraph(self) -> None:
        chunks = _text_doc_chunks()
        # Segment starts inside the Title heading, ends inside beta →
        # forward-scan picks beta (paragraph 1) for both ends.
        assert compute_paragraph_anchors(chunks, _TEXT, 7, 20) == (1, 1)

    def test_segment_with_no_paragraph_chunk_is_none(self) -> None:
        chunks = [
            _chunk(ChunkType.HEADING, "h1", 0),
            _chunk(ChunkType.HEADING, "h2", 1),
        ]
        assert compute_paragraph_anchors(chunks, _TEXT, 0, len("h1\n\nh2")) == (
            None,
            None,
        )

    def test_empty_chunks_returns_none(self) -> None:
        assert compute_paragraph_anchors([], _TEXT, 0, 5) == (None, None)

    def test_web_counts_every_chunk(self) -> None:
        # a\n\nbb\n\nccc — offsets a[0,3) bb[3,7) ccc[7,10)
        chunks = [
            _chunk(ChunkType.WEB_CONTENT, "a", 0),
            _chunk(ChunkType.WEB_CONTENT, "bb", 1),
            _chunk(ChunkType.WEB_CONTENT, "ccc", 2),
        ]
        assert compute_paragraph_anchors(chunks, _WEB, 0, 10) == (0, 2)
        assert compute_paragraph_anchors(chunks, _WEB, 3, 7) == (1, 1)
