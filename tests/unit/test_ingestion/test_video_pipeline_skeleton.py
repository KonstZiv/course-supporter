"""Unit tests for the video_pipeline skeleton VideoProcessor (Phase 2.4 task 2.4.1).

Drives the three base methods directly (no DB / orchestrator) on a mock
AuthoredDocument, exercising all 7 stub gnízda + their mock shapes, plus
the namespace-isolation gate (acceptance #3) and the per-step
failure-injection seam. The full orchestrator topology (state=ready,
persist cascade, R1 failure path) lives in the requires_db integration
test ``tests/integration/test_video_skeleton.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.models.source import ChunkType, SourceDocument, SourceType
from course_supporter.storage.orm import AuthoredDocument

_STEP4 = "course_supporter.ingestion.video_pipeline.steps.step_4_pass1_vision"


def _mock_authored_document() -> Mock:
    """AuthoredDocument stand-in (spec_set for attribute-access safety)."""
    doc = Mock(spec_set=AuthoredDocument)
    doc.source_url = "s3://bucket/lecture.mp4"
    doc.source_type = SourceType.VIDEO
    doc.filename = "lecture.mp4"
    return doc


class TestVideoProcessorConstruction:
    def test_takes_no_constructor_args(self) -> None:
        """Skeleton VideoProcessor wires with no dependencies."""
        assert isinstance(VideoProcessor(), VideoProcessor)


class TestVideoProcessorPipeline:
    """Happy-path coverage of the 7 stub gnízda over the 3 base methods."""

    async def test_process_raw_builds_video_source_document(self) -> None:
        """Krok 1-4 → SourceDocument(VIDEO) with transcript + visual chunks."""
        proc = VideoProcessor()

        doc = await proc.process_raw(_mock_authored_document())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        assert doc.source_url == "s3://bucket/lecture.mp4"
        assert doc.title == "lecture.mp4"
        chunk_types = {chunk.chunk_type for chunk in doc.chunks}
        assert ChunkType.TRANSCRIPT in chunk_types
        assert ChunkType.VISUAL_SCENE in chunk_types
        assert doc.assemble_text()  # non-empty reference text

    async def test_process_macro_returns_contiguous_segment_cover(self) -> None:
        """Krok 5 → DocumentSummaryDraft; segments start at 0 + contiguous."""
        proc = VideoProcessor()
        doc = await proc.process_raw(_mock_authored_document())

        summary = await proc.process_macro(doc, Mock())

        assert isinstance(summary, DocumentSummaryDraft)
        assert summary.segments  # mock reference text is comfortably long
        assert summary.segments[0].start_pos == 0
        prev_end = 0
        for seg in summary.segments:
            assert seg.start_pos == prev_end
            assert seg.end_pos > seg.start_pos
            prev_end = seg.end_pos
        assert prev_end == len(doc.assemble_text())
        # Last segment flagged noisy → drives the Pass 2c stub branch.
        assert summary.segments[-1].noisy is True

    async def test_process_detail_fills_content_and_cleans_noisy(self) -> None:
        """Krok 6-7 → every segment has content; noisy segments are cleaned."""
        proc = VideoProcessor()
        doc = await proc.process_raw(_mock_authored_document())
        reference = doc.assemble_text()
        summary = await proc.process_macro(doc, Mock())

        detail = await proc.process_detail(doc, summary)

        assert len(detail) == len(summary.segments)
        assert all(isinstance(seg, DocumentSegmentDraft) for seg in detail)
        assert all(seg.content for seg in detail)
        for seg in detail:
            if seg.noisy:
                assert seg.content is not None
                assert seg.content.startswith("[cleaned] ")
            else:
                assert seg.content == reference[seg.start_pos : seg.end_pos]

    async def test_full_pipeline_offline(self) -> None:
        """All 3 base methods chain end-to-end without external calls."""
        proc = VideoProcessor()
        source = _mock_authored_document()

        doc = await proc.process_raw(source)
        summary = await proc.process_macro(doc, Mock())
        detail = await proc.process_detail(doc, summary)

        assert detail
        assert all(seg.content for seg in detail)


class TestVideoProcessorFailureInjection:
    """Any gnízdo can raise ProcessingError (failure-injection seam)."""

    async def test_step_failure_propagates_processing_error(self) -> None:
        """Patching a gnízdo to raise surfaces ProcessingError to the caller."""
        proc = VideoProcessor()

        with (
            patch(
                _STEP4,
                new=AsyncMock(side_effect=ProcessingError("injected vision failure")),
            ),
            pytest.raises(ProcessingError, match="injected vision failure"),
        ):
            await proc.process_raw(_mock_authored_document())


class TestVideoPipelineIsolation:
    """Acceptance #3 — new namespace has zero ``course_supporter.vd`` imports."""

    def test_no_vd_imports_in_namespace(self) -> None:
        import course_supporter.ingestion.video_pipeline as pkg

        package_dir = Path(pkg.__file__).parent
        # Mirror acceptance #3 (``rg "from course_supporter.vd"``): match the
        # import statement forms, not bare docstring mentions of the module.
        import_forms = ("from course_supporter.vd", "import course_supporter.vd")
        offenders = [
            py.name
            for py in sorted(package_dir.glob("*.py"))
            if any(form in py.read_text(encoding="utf-8") for form in import_forms)
        ]
        assert offenders == [], (
            f"video_pipeline modules must not import course_supporter.vd; "
            f"offenders: {offenders}"
        )
