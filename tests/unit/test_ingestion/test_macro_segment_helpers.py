"""Unit tests for Stage 5 / 6 helpers (text + web)."""

from __future__ import annotations

import itertools

import pytest

from course_supporter.ingestion.macro_segment_helpers import (
    SnippetResolutionError,
    resolve_macro_sections,
    split_into_segments,
)
from course_supporter.models.macro_segment import MacroSectionCandidate


def _c(title: str, snippet: str) -> MacroSectionCandidate:
    return MacroSectionCandidate(title=title, start_snippet=snippet)


# ── resolve_macro_sections ──────────────────────────────────────────


class TestResolveMacroSections:
    def test_single_section_covers_full_document(self) -> None:
        text = "The full single-paragraph document content goes here " * 5
        drafts = resolve_macro_sections(
            candidates=[_c("All", text[:40])],
            processed_content=text,
        )
        assert len(drafts) == 1
        assert drafts[0].start_pos == 0
        assert drafts[0].end_pos == len(text)

    def test_three_contiguous_sections(self) -> None:
        intro = "Intro paragraph that introduces the subject.\n\n"
        functions = "## Functions\n\nA function is a block of code.\n\n"
        decorators = "## Decorators\n\nDecorators wrap functions and modify them."
        text = intro + functions + decorators
        drafts = resolve_macro_sections(
            candidates=[
                _c("Intro", intro[:35]),
                _c("Functions", functions[:35]),
                _c("Decorators", decorators[:35]),
            ],
            processed_content=text,
        )
        assert [d.order for d in drafts] == [0, 1, 2]
        # First section auto-extends to 0
        assert drafts[0].start_pos == 0
        # No gaps — section i.end_pos == section i+1.start_pos
        assert drafts[0].end_pos == drafts[1].start_pos
        assert drafts[1].end_pos == drafts[2].start_pos
        # Last section covers to end
        assert drafts[-1].end_pos == len(text)

    def test_gap_is_absorbed_into_previous_section(self) -> None:
        # Non-section noise between sections must be pulled into section 0.
        s0 = "Intro text spanning more than thirty characters here."
        noise = "\n\n  zzzzz noise between sections  \n\n"
        s1 = "Part two starts here and continues for a while."
        text = s0 + noise + s1
        drafts = resolve_macro_sections(
            candidates=[_c("Intro", s0[:35]), _c("Two", s1[:35])],
            processed_content=text,
        )
        # Section 0's end must reach the start of section 1 (gap absorbed).
        assert drafts[0].end_pos == drafts[1].start_pos
        # And section 1 covers until end of document.
        assert drafts[1].end_pos == len(text)

    def test_first_snippet_offset_is_extended_to_zero(self) -> None:
        preamble = "   \n\n<!-- auto-generated preamble -->\n\n"
        rest = "Section one body text that is long enough for a snippet."
        text = preamble + rest
        drafts = resolve_macro_sections(
            candidates=[_c("One", rest[:35])],
            processed_content=text,
        )
        assert drafts[0].start_pos == 0
        assert drafts[0].end_pos == len(text)

    def test_monotonic_search_disambiguates_repeated_snippet(self) -> None:
        # Same 40-char phrase appears twice — we must take the second
        # occurrence for the second section, not re-match the first.
        phrase = "Background information relevant to the "
        assert len(phrase) >= 30
        text = phrase + "topic A.\n\n---\n\n" + phrase + "topic B."
        drafts = resolve_macro_sections(
            candidates=[_c("A", phrase), _c("B", phrase)],
            processed_content=text,
        )
        assert drafts[0].start_pos == 0
        # Second match must land PAST the first match.
        assert drafts[1].start_pos > len(phrase)
        assert drafts[1].end_pos == len(text)

    def test_missing_snippet_raises(self) -> None:
        text = "Something that does not contain the snippet at all here."
        missing = "phrase that is absent from the document entirely"
        with pytest.raises(SnippetResolutionError, match="not found"):
            resolve_macro_sections(
                candidates=[_c("Bad", missing)],
                processed_content=text,
            )

    def test_empty_candidates_raises(self) -> None:
        with pytest.raises(ValueError, match="no macro sections"):
            resolve_macro_sections(candidates=[], processed_content="content")


# ── split_into_segments ─────────────────────────────────────────────


class TestSplitIntoSegments:
    def test_single_paragraph(self) -> None:
        content = "Short paragraph without blank lines."
        segs = split_into_segments(macro_content=content, macro_start_pos=500)
        assert len(segs) == 1
        assert segs[0].start_pos == 500
        assert segs[0].end_pos == 500 + len(content)
        assert segs[0].content == content

    def test_multiple_paragraphs(self) -> None:
        content = "First paragraph.\n\nSecond paragraph.\n\nThird one."
        segs = split_into_segments(macro_content=content, macro_start_pos=0)
        assert len(segs) == 3
        assert [s.order for s in segs] == [0, 1, 2]
        # Full coverage — no gap, no overlap.
        assert segs[0].start_pos == 0
        assert segs[-1].end_pos == len(content)
        for a, b in itertools.pairwise(segs):
            assert a.end_pos == b.start_pos

    def test_char_cap_splits_oversize_paragraph_on_whitespace(self) -> None:
        # One giant paragraph of 6000 chars with spaces every 50.
        chunk = ("word " * 10).strip()  # 49 chars, no trailing space
        content = (chunk + " ") * 122  # ~6100 chars, plenty of whitespace
        segs = split_into_segments(
            macro_content=content,
            macro_start_pos=0,
            max_chars=2000,
        )
        assert len(segs) >= 3
        # Each segment respects the cap (last may be shorter).
        for s in segs[:-1]:
            assert s.end_pos - s.start_pos <= 2000
        # Contiguous coverage.
        assert segs[0].start_pos == 0
        assert segs[-1].end_pos == len(content)
        for a, b in itertools.pairwise(segs):
            assert a.end_pos == b.start_pos

    def test_empty_content_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            split_into_segments(macro_content="", macro_start_pos=0)

    def test_absolute_positions_respect_macro_offset(self) -> None:
        content = "Para one.\n\nPara two."
        segs = split_into_segments(macro_content=content, macro_start_pos=1000)
        # start/end are ABSOLUTE into the full processed_content,
        # not relative to macro_content.
        assert segs[0].start_pos == 1000
        assert segs[-1].end_pos == 1000 + len(content)
