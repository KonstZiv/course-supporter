"""Unit tests for TextProcessor.process_macro (Phase 2.1 C5, KD-2.1-A).

Verifies Pass 2a behaviour for text materials with a mocked
StageRouter -- no real LLM calls. Pivot 1 signals (JSON parse, concept
quality, latency) surface only against real LLMs and are handled via
manual smoke + ratify per pre-flight section 3.E.

Fixup 2.1.7.2 wired the coverage closure as ``response_validator``
on :meth:`StageRouter.execute_for_stage`, so the fake router below
invokes the closure on its canned payload before returning a
:class:`StageResult` -- mirroring production semantics (validator
runs inside the attempt scope, raises StructuralRetryError on
schema mismatch).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.ingestion.text import TextProcessor
from course_supporter.llm.error_categories import StructuralRetryError
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


def _router_returning(payload: str) -> AsyncMock:
    """StageRouter mock that invokes ``response_validator`` (fixup 2.1.7.2).

    The closure passed by ``process_macro`` is the only place where
    Pydantic ``model_validate_json`` runs in the refactored flow,
    so the mock must call it on the canned payload before returning
    a :class:`StageResult`. Any ``StructuralRetryError`` raised by
    the closure propagates as-is (no router-level retry simulated
    in unit tests).
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
        processor = TextProcessor()
        doc = _make_doc("Heading paragraph.", "Body paragraph two.")
        # v2 prompt does NOT ask LLM for document-level concepts —
        # they are computed post-LLM as a union over segments.
        router = _router_returning(
            '{"title": "Sample", "description": "A test document.", "segments": []}'
        )

        draft = await processor.process_macro(doc, router)

        assert isinstance(draft, DocumentSummaryDraft)
        assert draft.title == "Sample"
        # No segments → aggregation yields empty lists.
        assert draft.main_concepts == []
        assert draft.secondary_concepts == []
        router.execute_for_stage.assert_awaited_once()
        call_kwargs = router.execute_for_stage.await_args.kwargs
        assert call_kwargs["text"] == "Heading paragraph.\n\nBody paragraph two."
        assert call_kwargs["response_validator"] is not None
        assert router.execute_for_stage.await_args.args == ("pass_2a_mapping",)

    @pytest.mark.asyncio
    async def test_segments_propagate_into_draft(self) -> None:
        """KD-2.1-O: segments carry metadata only; ``content`` stays None."""
        processor = TextProcessor()
        doc = _make_doc("alpha.", "beta.")
        # Contiguous segments per fixup 2.1.7.1 invariants (no gaps; first
        # starts at 0; last segment ends at total text length — matches
        # the reference text length passed via Pydantic context per
        # fixup 2.1.7.2).
        router = _router_returning(
            '{"title": "T", "description": "D",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 6,'
            '    "title": "Alpha section",'
            '    "description": "Frames the alpha topic.",'
            '    "main_concepts": ["a"], "secondary_concepts": []},'
            '   {"order": 1, "start_pos": 6, "end_pos": 13,'
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
        # assemble_text() = "a\n\nb\n\nc" (7 chars). Contiguous cover
        # 0..7 split per fixup 2.1.7.1; last end_pos matches the
        # reference text length passed via Pydantic context.
        doc = _make_doc("a", "b", "c")
        router = _router_returning(
            '{"title": "T", "description": "D",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 3,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": ["A", "B"], "secondary_concepts": ["X"]},'
            '   {"order": 1, "start_pos": 3, "end_pos": 5,'
            '    "title": null, "description": "d1",'
            '    "main_concepts": ["B", "C"], "secondary_concepts": ["Y"]},'
            '   {"order": 2, "start_pos": 5, "end_pos": 7,'
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
        # assemble_text() = "a\n\nb" (4 chars).
        doc = _make_doc("a", "b")
        router = _router_returning(
            '{"title": "T", "description": "D",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 2,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": ["yield", "generator"],'
            '    "secondary_concepts": ["StopIteration"]},'
            '   {"order": 1, "start_pos": 2, "end_pos": 4,'
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

    @pytest.mark.asyncio
    async def test_concepts_aggregation_consolidates_spelling_variants(self) -> None:
        """concept-quality phase 1: cross-segment spelling variants collapse.

        "HTML Template" (segment 0 main) and "HTML templates" (segment 1 main)
        are variants of one concept split across two segments — only the
        document-level merge can consolidate them, since the segment validator
        sees each segment alone. The secondary variant "HTML template" is
        dropped by the conflict rule on the normalization key, not the exact
        string.
        """
        processor = TextProcessor()
        # assemble_text() = "a\n\nb" (4 chars).
        doc = _make_doc("a", "b")
        router = _router_returning(
            '{"title": "T", "description": "D",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 2,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": ["HTML Template"], "secondary_concepts": []},'
            '   {"order": 1, "start_pos": 2, "end_pos": 4,'
            '    "title": null, "description": "d1",'
            '    "main_concepts": ["HTML templates"],'
            '    "secondary_concepts": ["HTML template"]}'
            " ]}"
        )

        draft = await processor.process_macro(doc, router)

        # Variants collapse to one verbatim winner (tie -> first occurrence).
        assert draft.main_concepts == ["HTML Template"]
        # The secondary variant is removed by the key-based conflict rule.
        assert draft.secondary_concepts == []


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
    """LLM output that fails Pydantic validation surfaces a StructuralRetryError.

    Fixup 2.1.7.2 moved validation into a StageRouter ``response_validator``
    closure: the closure translates ``ValidationError`` to
    :class:`StructuralRetryError` so the router's instructor-style retry
    + ladder fallback can fire. In unit tests the mocked router does not
    simulate retry, so the exception propagates as-is for the test
    surface. The original ``ValidationError`` is preserved as
    ``__cause__`` for debugging.
    """

    @pytest.mark.asyncio
    async def test_missing_required_field_surfaces_structural_retry(
        self,
    ) -> None:
        from pydantic import ValidationError

        processor = TextProcessor()
        doc = _make_doc("hello")
        router = _router_returning(
            '{"title": "T",'
            ' "main_concepts": [], "secondary_concepts": [],'
            ' "segments": []}'
        )

        with pytest.raises(StructuralRetryError) as exc_info:
            await processor.process_macro(doc, router)
        # Original ValidationError preserved as cause for debugging.
        assert isinstance(exc_info.value.__cause__, ValidationError)

    @pytest.mark.asyncio
    async def test_coverage_mismatch_surfaces_structural_retry(self) -> None:
        """Reference text length passed via Pydantic context catches
        a LLM-emitted ``segments[-1].end_pos`` that overshoots / undershoots
        the deterministic document length."""
        from pydantic import ValidationError

        processor = TextProcessor()
        # assemble_text() = "hello world" (11 chars).
        doc = _make_doc("hello world")
        # end_pos=20 overshoots the 11-char reference document.
        router = _router_returning(
            '{"title": "T", "description": "D",'
            ' "segments": ['
            '   {"order": 0, "start_pos": 0, "end_pos": 20,'
            '    "title": null, "description": "d0",'
            '    "main_concepts": [], "secondary_concepts": []}'
            " ]}"
        )

        with pytest.raises(StructuralRetryError) as exc_info:
            await processor.process_macro(doc, router)
        assert isinstance(exc_info.value.__cause__, ValidationError)
        # Feedback message should be actionable (mention coverage).
        cause = exc_info.value.__cause__
        assert any(
            "cover" in err.get("msg", "")
            or "reference_text_length" in err.get("msg", "")
            for err in cause.errors()
        )
