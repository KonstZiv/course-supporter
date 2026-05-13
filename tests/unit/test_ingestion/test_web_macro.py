"""Unit tests for WebProcessor.process_macro (Phase 2.1 C5, KD-2.1-A).

Mirror of test_text_macro.py for the web ingestion path. Web pages
produce ContentChunk.WEB_CONTENT entries from the scraped body; the
Pass 2a contract is otherwise identical to text materials.

Fixup 2.1.7.2 wired the coverage closure as ``response_validator``
on :meth:`StageRouter.execute_for_stage`; the fake router here
mirrors that wiring so the closure runs on the canned payload
before the test reads the returned draft.
"""

from __future__ import annotations

from typing import Any
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


def _router_returning(payload: str) -> AsyncMock:
    """StageRouter mock that invokes ``response_validator`` (fixup 2.1.7.2).

    Mirrors :func:`tests.unit.test_ingestion.test_text_macro._router_returning`.
    """
    router = AsyncMock()

    async def _fake_execute(
        _stage_name: str,
        *,
        response_validator: Any | None = None,
        **_render_context: Any,
    ) -> StageResult:
        if response_validator is not None:
            response_validator(payload)
        return _stage_result(payload)

    router.execute_for_stage.side_effect = _fake_execute
    return router


class TestProcessMacroHappyPath:
    """Successful Pass 2a path with valid LLM JSON output."""

    @pytest.mark.asyncio
    async def test_returns_document_summary_draft(self) -> None:
        """LLM returns document-level metadata; concepts aggregate from segments."""
        processor = WebProcessor()
        # assemble_text() = "Lead paragraph.\n\nFollow-up content." (35 chars).
        doc = _make_doc("Lead paragraph.", "Follow-up content.")
        router = _router_returning(
            '{"title": "Article", "description": "Brief web article.",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 35,'
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
        call_kwargs = router.execute_for_stage.await_args.kwargs
        assert call_kwargs["text"] == "Lead paragraph.\n\nFollow-up content."
        assert call_kwargs["response_validator"] is not None
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
