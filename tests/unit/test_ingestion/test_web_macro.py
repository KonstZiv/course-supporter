"""Unit tests for WebProcessor.process_macro (Phase 2.1 C5, KD-2.1-A).

Mirror of test_text_macro.py for the web ingestion path. Web pages
produce ContentChunk.WEB_CONTENT entries from the scraped body; the
Pass 2a contract is otherwise identical to text materials.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.ingestion.web import WebProcessor
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


def _make_doc(*chunk_texts: str) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.WEB,
        source_url="https://example.com/article",
        title="article",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.WEB_CONTENT,
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
        processor = WebProcessor()
        doc = _make_doc("Lead paragraph.", "Follow-up content.")
        router = AsyncMock()
        # v2 prompt does NOT ask LLM for document-level concepts —
        # they are computed post-LLM as a union over segments. With
        # empty segments the aggregation result is an empty list.
        router.execute_for_stage.return_value = _stage_result(
            '{"title": "Article", "description": "Brief web article.",'
            ' "content_char_count": 34,'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 34,'
            '    "title": null, "description": "Single-segment article body.",'
            '    "main_concepts": ["topic"], "secondary_concepts": ["aside"]}'
            " ]}"
        )

        draft = await processor.process_macro(doc, router)

        assert isinstance(draft, DocumentSummaryDraft)
        assert draft.title == "Article"
        # Aggregated from the single segment.
        assert draft.main_concepts == ["topic"]
        assert draft.secondary_concepts == ["aside"]
        assert draft.content_char_count == 34
        call_kwargs = router.execute_for_stage.await_args.kwargs
        assert call_kwargs["text"] == "Lead paragraph.\n\nFollow-up content."
        assert router.execute_for_stage.await_args.args == ("pass_2a_mapping",)


class TestProcessMacroEmptyDocument:
    """Empty scrape result is rejected before invoking the LLM."""

    @pytest.mark.asyncio
    async def test_empty_chunks_raises_processing_error(self) -> None:
        processor = WebProcessor()
        doc = SourceDocument(
            source_type=SourceType.WEB,
            source_url="https://example.com/empty",
            title="empty",
            chunks=[],
        )
        router = AsyncMock()

        with pytest.raises(ProcessingError, match="empty document"):
            await processor.process_macro(doc, router)
        router.execute_for_stage.assert_not_awaited()
