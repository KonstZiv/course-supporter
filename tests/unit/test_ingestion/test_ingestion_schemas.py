"""Unit tests for ingestion draft Pydantic models (KD-2.1-M, KD-2.1-O).

Covers :class:`DocumentSummaryDraft` + :class:`DocumentSegmentDraft`
added in Phase 2.1 commit 4.5. Both are in-flight Pydantic carriers
returned by ``MaterialProcessor.process_macro`` / ``process_detail``
abstract methods; concrete TextProcessor / WebProcessor overrides
land in commits 5 and 7.

KD-2.1-O (ratified 2026-05-12) makes ``DocumentSegmentDraft.content``
optional with default ``None``: Pass 2a for text/web emits metadata
only; Pass 2b (C7) fills ``content`` algorithmically from
``doc.text[start_pos:end_pos]``. For audio/video Pass 2a may set
``content`` directly. Both code paths must validate.

Tests verify:

* Required (non-list) fields raise ``ValidationError`` when omitted
  -- fail-fast against partial LLM output (KD-2.1-M strictness).
* List fields default to empty list when omitted.
* Nested draft structures roundtrip faithfully.
* ``content`` accepts ``None`` (text/web Pass 2a) AND non-empty
  ``str`` (audio/video Pass 2a, or Pass 2b post-slicing). Default
  is ``None``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)


class TestDocumentSegmentDraft:
    """Validation contract for DocumentSegmentDraft."""

    def test_minimal_required_fields_accepted(self) -> None:
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=42,
            description="Defines the iterator protocol.",
        )
        assert draft.order == 0
        assert draft.start_pos == 0
        assert draft.end_pos == 42
        assert draft.description == "Defines the iterator protocol."
        assert draft.title is None
        assert draft.content is None
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []

    def test_concept_lists_preserved(self) -> None:
        draft = DocumentSegmentDraft(
            order=1,
            start_pos=10,
            end_pos=20,
            description="Walks through lexer + parser stages.",
            main_concepts=["lexer", "parser"],
            secondary_concepts=["AST"],
        )
        assert draft.main_concepts == ["lexer", "parser"]
        assert draft.secondary_concepts == ["AST"]

    def test_content_none_default(self) -> None:
        """KD-2.1-O text/web path: Pass 2a does NOT emit segment content."""
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=80,
            description="Introduces the generator protocol.",
        )
        assert draft.content is None

    def test_content_explicit_none_accepted(self) -> None:
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=80,
            description="Introduces the generator protocol.",
            content=None,
        )
        assert draft.content is None

    def test_content_string_accepted(self) -> None:
        """audio/video Pass 2a OR Pass 2b post-slicing populates content."""
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=20,
            description="Spoken intro defining the lecture topic.",
            content="The quick brown fox.",
        )
        assert draft.content == "The quick brown fox."

    def test_title_optional_defaults_none(self) -> None:
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=10,
            description="Boilerplate copyright section.",
        )
        assert draft.title is None

    def test_title_string_accepted(self) -> None:
        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=10,
            title="Introduction",
            description="Frames the broader topic.",
        )
        assert draft.title == "Introduction"

    @pytest.mark.parametrize(
        "missing",
        ["order", "start_pos", "end_pos", "description"],
    )
    def test_missing_required_field_raises(self, missing: str) -> None:
        kwargs: dict[str, object] = {
            "order": 0,
            "start_pos": 0,
            "end_pos": 1,
            "description": "Default fixture description.",
        }
        del kwargs[missing]
        with pytest.raises(ValidationError) as exc_info:
            DocumentSegmentDraft(**kwargs)
        assert missing in str(exc_info.value)


class TestDocumentSummaryDraft:
    """Validation contract for DocumentSummaryDraft."""

    def test_minimal_required_fields_accepted(self) -> None:
        draft = DocumentSummaryDraft(
            title="Intro to Compilers",
            description="Five-week module on parser construction.",
            content_char_count=1234,
        )
        assert draft.title == "Intro to Compilers"
        assert draft.description == "Five-week module on parser construction."
        assert draft.content_char_count == 1234
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []
        assert draft.segments == []

    def test_concept_lists_preserved(self) -> None:
        draft = DocumentSummaryDraft(
            title="Sample",
            description="Sample document.",
            content_char_count=10,
            main_concepts=["concept_a"],
            secondary_concepts=["concept_b", "concept_c"],
        )
        assert draft.main_concepts == ["concept_a"]
        assert draft.secondary_concepts == ["concept_b", "concept_c"]

    def test_segments_roundtrip(self) -> None:
        seg = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=5,
            description="Opens with a greeting.",
            main_concepts=["greeting"],
        )
        draft = DocumentSummaryDraft(
            title="t",
            description="d",
            content_char_count=5,
            segments=[seg],
        )
        assert len(draft.segments) == 1
        assert draft.segments[0].description == "Opens with a greeting."
        assert draft.segments[0].content is None
        assert draft.segments[0].main_concepts == ["greeting"]

    @pytest.mark.parametrize(
        "missing",
        ["title", "description", "content_char_count"],
    )
    def test_missing_required_field_raises(self, missing: str) -> None:
        kwargs: dict[str, object] = {
            "title": "t",
            "description": "d",
            "content_char_count": 0,
        }
        del kwargs[missing]
        with pytest.raises(ValidationError) as exc_info:
            DocumentSummaryDraft(**kwargs)
        assert missing in str(exc_info.value)

    def test_none_for_required_field_rejected(self) -> None:
        # Fail-fast strictness: None into required str must not be coerced.
        with pytest.raises(ValidationError):
            DocumentSummaryDraft(
                title=None,  # type: ignore[arg-type]
                description="d",
                content_char_count=0,
            )

    def test_model_validate_json_without_doc_level_concepts(self) -> None:
        """v2 prompt omits document-level concepts; defaults must be ``[]``.

        TextProcessor / WebProcessor ``process_macro`` parse LLM
        responses via ``model_validate_json``. The v2 prompt (KD-2.1-O)
        no longer asks the LLM for document-level ``main_concepts`` /
        ``secondary_concepts`` — the processor aggregates them from
        segments post-parse. The schema must therefore accept missing
        keys and default them to an empty list so the aggregation step
        has a known starting point.
        """
        payload = (
            '{"title": "Sample", "description": "Sample doc.",'
            ' "content_char_count": 0, "segments": []}'
        )
        draft = DocumentSummaryDraft.model_validate_json(payload)
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []


class TestDocumentSegmentDraftOffsetInvariants:
    """Fixup 2.1.7.1 — strict offset invariants per prompt v1.md."""

    def test_inverted_offsets_rejected(self) -> None:
        """end_pos must be strictly greater than start_pos."""
        with pytest.raises(ValidationError, match=r"end_pos.*start_pos"):
            DocumentSegmentDraft(
                order=0,
                start_pos=100,
                end_pos=50,
                description="Inverted offsets — order=0 case.",
            )

    def test_equal_offsets_rejected(self) -> None:
        """end_pos == start_pos is a zero-length segment; not allowed."""
        with pytest.raises(ValidationError, match=r"end_pos.*start_pos"):
            DocumentSegmentDraft(
                order=0,
                start_pos=42,
                end_pos=42,
                description="Zero-length window.",
            )

    def test_negative_start_pos_rejected(self) -> None:
        with pytest.raises(ValidationError, match="start_pos must be non-negative"):
            DocumentSegmentDraft(
                order=0,
                start_pos=-1,
                end_pos=5,
                description="Negative start.",
            )

    def test_negative_order_rejected(self) -> None:
        with pytest.raises(ValidationError, match="order must be non-negative"):
            DocumentSegmentDraft(
                order=-1,
                start_pos=0,
                end_pos=5,
                description="Negative order.",
            )


class TestDocumentSummaryDraftSequenceInvariants:
    """Fixup 2.1.7.1 — strict cover invariants per prompt v1.md."""

    @staticmethod
    def _seg(order: int, start: int, end: int) -> DocumentSegmentDraft:
        return DocumentSegmentDraft(
            order=order,
            start_pos=start,
            end_pos=end,
            description=f"Segment {order} fixture.",
        )

    def test_empty_segments_accepted(self) -> None:
        """Trivially short doc — segments=[] allowed."""
        draft = DocumentSummaryDraft(
            title="t", description="d", content_char_count=0, segments=[]
        )
        assert draft.segments == []

    def test_contiguous_cover_accepted(self) -> None:
        """0..N contiguous segments matching content_char_count — happy path."""
        draft = DocumentSummaryDraft(
            title="t",
            description="d",
            content_char_count=30,
            segments=[
                self._seg(0, 0, 10),
                self._seg(1, 10, 20),
                self._seg(2, 20, 30),
            ],
        )
        assert len(draft.segments) == 3

    def test_first_segment_must_start_at_zero(self) -> None:
        with pytest.raises(ValidationError, match="first segment must start at 0"):
            DocumentSummaryDraft(
                title="t",
                description="d",
                content_char_count=20,
                segments=[
                    self._seg(0, 5, 10),
                    self._seg(1, 10, 20),
                ],
            )

    def test_segment_order_must_be_strictly_monotonic(self) -> None:
        """Order [0, 2] (gap) rejected."""
        with pytest.raises(ValidationError, match="strictly monotonic"):
            DocumentSummaryDraft(
                title="t",
                description="d",
                content_char_count=20,
                segments=[
                    self._seg(0, 0, 10),
                    self._seg(2, 10, 20),
                ],
            )

    def test_segments_must_not_overlap(self) -> None:
        """prev.end_pos > next.start_pos rejected."""
        with pytest.raises(ValidationError, match="contiguous without gaps or overlap"):
            DocumentSummaryDraft(
                title="t",
                description="d",
                content_char_count=20,
                segments=[
                    self._seg(0, 0, 12),
                    self._seg(1, 10, 20),  # overlaps prev
                ],
            )

    def test_segments_must_not_have_gaps(self) -> None:
        """prev.end_pos < next.start_pos rejected."""
        with pytest.raises(ValidationError, match="contiguous without gaps or overlap"):
            DocumentSummaryDraft(
                title="t",
                description="d",
                content_char_count=20,
                segments=[
                    self._seg(0, 0, 8),
                    self._seg(1, 10, 20),  # gap between 8 and 10
                ],
            )

    def test_content_char_count_must_match_last_end_pos(self) -> None:
        """content_char_count == segments[-1].end_pos required."""
        with pytest.raises(
            ValidationError, match=r"content_char_count.*last segment end_pos"
        ):
            DocumentSummaryDraft(
                title="t",
                description="d",
                content_char_count=25,
                segments=[
                    self._seg(0, 0, 10),
                    self._seg(1, 10, 20),  # last end=20 != count=25
                ],
            )

    def test_negative_content_char_count_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="content_char_count must be non-negative"
        ):
            DocumentSummaryDraft(
                title="t", description="d", content_char_count=-1, segments=[]
            )

    def test_real_world_smoke_a_bug_rejected(self) -> None:
        """Regression: C8 smoke A 2026-05-13 LLM output (end_pos < start_pos)."""
        with pytest.raises(ValidationError, match=r"end_pos.*start_pos"):
            DocumentSegmentDraft(
                order=13,
                start_pos=8700,
                end_pos=6187,
                description="LLM-emitted malformed offsets (real bug data).",
            )
