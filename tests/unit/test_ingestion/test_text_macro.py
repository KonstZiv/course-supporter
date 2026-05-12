"""Unit tests for TextProcessor.process_macro (Phase 2.1 C5, KD-2.1-A).

Verifies Pass 2a behaviour for text materials with a mocked
StageRouter -- no real LLM calls. Pivot 1 signals (JSON parse, concept
quality, latency) surface only against real LLMs and are handled via
manual smoke + ratify per pre-flight section 3.E.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.ingestion.text import TextProcessor
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


def _make_doc(*chunk_texts: str) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.TEXT,
        source_url="file:///fixture.md",
        title="fixture",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.PARAGRAPH,
                text=text,
                index=idx,
            )
            for idx, text in enumerate(chunk_texts)
        ],
    )


def _stage_result(payload: str) -> StageResult:
    return StageResult(
        content=payload,
        provider_used="gemini",
        model_used="gemini-2.5-pro",
        attempt_count=1,
    )


class TestProcessMacroHappyPath:
    """Successful Pass 2a path with valid LLM JSON output."""

    @pytest.mark.asyncio
    async def test_returns_document_summary_draft(self) -> None:
        """LLM returns document-level metadata; concepts aggregate from segments."""
        processor = TextProcessor()
        doc = _make_doc("Heading paragraph.", "Body paragraph two.")
        router = AsyncMock()
        # v2 prompt does NOT ask LLM for document-level concepts —
        # they are computed post-LLM as a union over segments.
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "Sample", "description": "A test document.",'
            ' "content_char_count": 36, "segments": []}'
        )

        draft = await processor.process_macro(doc, router)

        assert isinstance(draft, DocumentSummaryDraft)
        assert draft.title == "Sample"
        # No segments → aggregation yields empty lists.
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []
        assert draft.content_char_count == 36
        router.execute_for_stage.assert_awaited_once()
        call_kwargs = router.execute_for_stage.await_args.kwargs
        assert call_kwargs["text"] == "Heading paragraph.\n\nBody paragraph two."
        assert router.execute_for_stage.await_args.args == ("pass_2a_mapping",)

    @pytest.mark.asyncio
    async def test_segments_propagate_into_draft(self) -> None:
        """KD-2.1-O: segments carry metadata only; ``content`` stays None."""
        processor = TextProcessor()
        doc = _make_doc("alpha.", "beta.")
        router = AsyncMock()
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "T", "description": "D",'
            ' "content_char_count": 13,'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 6,'
            '    "title": "Alpha section",'
            '    "description": "Frames the alpha topic.",'
            '    "main_concepts": ["a"], "secondary_concepts": []},'
            '   {"order": 1, "start_pos": 8, "end_pos": 13,'
            '    "title": null,'
            '    "description": "Follow-up on beta.",'
            '    "main_concepts": ["b"], "secondary_concepts": []}'
            " ]}"
        )

        draft = await processor.process_macro(doc, router)

        assert len(draft.segments) == 2
        assert draft.segments[0].title == "Alpha section"
        assert draft.segments[0].description == "Frames the alpha topic."
        assert draft.segments[0].content is None
        assert draft.segments[1].title is None
        assert draft.segments[1].main_concepts == ["b"]
        assert draft.segments[1].content is None


class TestProcessMacroConceptsAggregation:
    """KD-2.1-O / vision.md §2.2: document-level concepts are union over segments."""

    @pytest.mark.asyncio
    async def test_concepts_aggregation_from_segments(self) -> None:
        """Sorted union of segment concepts populates document-level fields."""
        processor = TextProcessor()
        doc = _make_doc("a", "b", "c")
        router = AsyncMock()
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "T", "description": "D",'
            ' "content_char_count": 30,'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 10,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": ["A", "B"], "secondary_concepts": ["X"]},'
            '   {"order": 1, "start_pos": 10, "end_pos": 20,'
            '    "title": null, "description": "d1",'
            '    "main_concepts": ["B", "C"], "secondary_concepts": ["Y"]},'
            '   {"order": 2, "start_pos": 20, "end_pos": 30,'
            '    "title": null, "description": "d2",'
            '    "main_concepts": ["D"], "secondary_concepts": ["Z"]}'
            " ]}"
        )

        draft = await processor.process_macro(doc, router)

        assert draft.main_concepts == ["A", "B", "C", "D"]
        assert draft.secondary_concepts == ["X", "Y", "Z"]

    @pytest.mark.asyncio
    async def test_concepts_aggregation_main_wins_over_secondary(self) -> None:
        """Conflict rule: a concept that is main anywhere stays in main."""
        processor = TextProcessor()
        doc = _make_doc("a", "b")
        router = AsyncMock()
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "T", "description": "D",'
            ' "content_char_count": 20,'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 10,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": ["yield", "generator"],'
            '    "secondary_concepts": ["StopIteration"]},'
            '   {"order": 1, "start_pos": 10, "end_pos": 20,'
            '    "title": null, "description": "d1",'
            '    "main_concepts": ["itertools"],'
            '    "secondary_concepts": ["yield"]}'
            " ]}"
        )

        draft = await processor.process_macro(doc, router)

        # "yield" is main in segment 0 + secondary in segment 1 — main wins.
        assert draft.main_concepts == ["generator", "itertools", "yield"]
        # "yield" must NOT appear here.
        assert draft.secondary_concepts == ["StopIteration"]


class TestProcessMacroEmptyDocument:
    """Empty document is rejected before invoking the LLM."""

    @pytest.mark.asyncio
    async def test_empty_chunks_raises_processing_error(self) -> None:
        processor = TextProcessor()
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///empty.md",
            title="empty",
            chunks=[],
        )
        router = AsyncMock()

        with pytest.raises(ProcessingError, match="empty document"):
            await processor.process_macro(doc, router)
        router.execute_for_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_chunk_text_raises_processing_error(self) -> None:
        processor = TextProcessor()
        doc = _make_doc("   ", "\n\n")
        router = AsyncMock()

        with pytest.raises(ProcessingError, match="empty document"):
            await processor.process_macro(doc, router)
        router.execute_for_stage.assert_not_awaited()


class TestProcessMacroValidationError:
    """LLM output that fails Pydantic validation propagates the error."""

    @pytest.mark.asyncio
    async def test_missing_required_field_propagates_validation_error(
        self,
    ) -> None:
        from pydantic import ValidationError

        processor = TextProcessor()
        doc = _make_doc("hello")
        router = AsyncMock()
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "T",'
            ' "main_concepts": [], "secondary_concepts": [],'
            ' "content_char_count": 5, "segments": []}'
        )

        with pytest.raises(ValidationError):
            await processor.process_macro(doc, router)
