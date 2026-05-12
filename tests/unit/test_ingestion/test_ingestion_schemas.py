"""Unit tests for ingestion draft Pydantic models (KD-2.1-M).

Covers :class:`DocumentSummaryDraft` + :class:`DocumentSegmentDraft`
added in Phase 2.1 commit 4.5. Both are in-flight Pydantic carriers
returned by ``MaterialProcessor.process_macro`` / ``process_detail``
abstract methods; concrete TextProcessor / WebProcessor overrides
land in commits 5 and 7.

Tests verify:

* Required (non-list) fields raise ``ValidationError`` when omitted
  -- fail-fast against partial LLM output (KD-2.1-M strictness).
* List fields default to empty list when omitted.
* Nested draft structures roundtrip faithfully.
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
            content="The quick brown fox.",
        )
        assert draft.order == 0
        assert draft.start_pos == 0
        assert draft.end_pos == 42
        assert draft.content == "The quick brown fox."
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []

    def test_concept_lists_preserved(self) -> None:
        draft = DocumentSegmentDraft(
            order=1,
            start_pos=10,
            end_pos=20,
            content="payload",
            main_concepts=["lexer", "parser"],
            secondary_concepts=["AST"],
        )
        assert draft.main_concepts == ["lexer", "parser"]
        assert draft.secondary_concepts == ["AST"]

    @pytest.mark.parametrize(
        "missing",
        ["order", "start_pos", "end_pos", "content"],
    )
    def test_missing_required_field_raises(self, missing: str) -> None:
        kwargs: dict[str, object] = {
            "order": 0,
            "start_pos": 0,
            "end_pos": 1,
            "content": "x",
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
            content="hello",
            main_concepts=["greeting"],
        )
        draft = DocumentSummaryDraft(
            title="t",
            description="d",
            content_char_count=5,
            segments=[seg],
        )
        assert len(draft.segments) == 1
        assert draft.segments[0].content == "hello"
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
