"""Tests for MergeStep."""

import pytest

from course_supporter.ingestion.merge import MergeStep
from course_supporter.models.course import CourseContext, SlideTimecodeRef
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


def _make_doc(
    source_type: SourceType = SourceType.TEXT,
    chunks: list[ContentChunk] | None = None,
) -> SourceDocument:
    """Create a minimal SourceDocument for testing."""
    return SourceDocument(
        source_type=source_type,
        source_url=f"file:///test.{source_type}",
        title=f"Test {source_type}",
        chunks=chunks or [],
    )


def _make_slide_chunk(slide_number: int, text: str = "Slide text") -> ContentChunk:
    """Create a SLIDE_TEXT chunk with slide_number metadata."""
    return ContentChunk(
        chunk_type=ChunkType.SLIDE_TEXT,
        text=text,
        index=slide_number,
        metadata={"slide_number": slide_number},
    )


class TestMergeStep:
    def test_single_document(self) -> None:
        """One document -> CourseContext with 1 document."""
        step = MergeStep()
        doc = _make_doc()

        result = step.merge([doc])

        assert isinstance(result, CourseContext)
        assert len(result.documents) == 1
        assert result.documents[0].source_type == SourceType.TEXT

    def test_multiple_documents(self) -> None:
        """Video + text -> CourseContext with 2 documents."""
        step = MergeStep()
        video = _make_doc(SourceType.VIDEO)
        text = _make_doc(SourceType.TEXT)

        result = step.merge([video, text])

        assert len(result.documents) == 2

    def test_empty_documents_raises(self) -> None:
        """Empty documents list -> ValueError."""
        step = MergeStep()

        with pytest.raises(ValueError, match="Cannot merge empty documents list"):
            step.merge([])

    def test_document_ordering(self) -> None:
        """Documents sorted by priority: video -> presentation -> text -> web."""
        step = MergeStep()
        web = _make_doc(SourceType.WEB)
        video = _make_doc(SourceType.VIDEO)
        text = _make_doc(SourceType.TEXT)
        presentation = _make_doc(SourceType.PRESENTATION)

        result = step.merge([web, text, presentation, video])

        types = [d.source_type for d in result.documents]
        assert types == [
            SourceType.VIDEO,
            SourceType.PRESENTATION,
            SourceType.TEXT,
            SourceType.WEB,
        ]

    def test_no_mappings_default(self) -> None:
        """No mappings -> empty list in CourseContext."""
        step = MergeStep()

        result = step.merge([_make_doc()])

        assert result.slide_video_mappings == []

    def test_with_mappings(self) -> None:
        """Documents + mappings passed through to CourseContext."""
        step = MergeStep()
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
            SlideTimecodeRef(slide_number=2, video_timecode_start="00:05:30"),
        ]

        result = step.merge([_make_doc()], mappings=mappings)

        assert len(result.slide_video_mappings) == 2
        assert result.slide_video_mappings[0].video_timecode_start == "00:01:00"

    def test_cross_references(self) -> None:
        """Presentation SLIDE_TEXT chunks enriched with video_timecode."""
        step = MergeStep()
        presentation = _make_doc(
            SourceType.PRESENTATION,
            chunks=[_make_slide_chunk(1), _make_slide_chunk(2)],
        )
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
            SlideTimecodeRef(slide_number=2, video_timecode_start="00:05:30"),
        ]

        result = step.merge([presentation], mappings=mappings)

        chunks = result.documents[0].chunks
        assert chunks[0].metadata["video_timecode"] == "00:01:00"
        assert chunks[1].metadata["video_timecode"] == "00:05:30"

    def test_cross_references_preserve_original_metadata(self) -> None:
        """Cross-referencing preserves existing chunk metadata."""
        step = MergeStep()
        presentation = _make_doc(
            SourceType.PRESENTATION,
            chunks=[_make_slide_chunk(1)],
        )
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
        ]

        result = step.merge([presentation], mappings=mappings)

        chunk = result.documents[0].chunks[0]
        assert chunk.metadata["slide_number"] == 1
        assert chunk.metadata["video_timecode"] == "00:01:00"

    def test_cross_references_skip_non_presentation(self) -> None:
        """Non-presentation documents not affected by cross-references."""
        step = MergeStep()
        video = _make_doc(
            SourceType.VIDEO,
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text="Transcript",
                    index=0,
                )
            ],
        )
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
        ]

        result = step.merge([video], mappings=mappings)

        assert "video_timecode" not in result.documents[0].chunks[0].metadata

    def test_cross_references_unmatched_slide(self) -> None:
        """Slides without matching mapping keep original metadata."""
        step = MergeStep()
        presentation = _make_doc(
            SourceType.PRESENTATION,
            chunks=[_make_slide_chunk(3)],
        )
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
        ]

        result = step.merge([presentation], mappings=mappings)

        chunk = result.documents[0].chunks[0]
        assert "video_timecode" not in chunk.metadata
        assert chunk.metadata["slide_number"] == 3

    def test_cross_references_skip_slide_description(self) -> None:
        """SLIDE_DESCRIPTION chunks not enriched even with matching slide_number."""
        step = MergeStep()
        presentation = _make_doc(
            SourceType.PRESENTATION,
            chunks=[
                _make_slide_chunk(1),
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_DESCRIPTION,
                    text="Diagram showing flow",
                    index=1,
                    metadata={"slide_number": 1},
                ),
            ],
        )
        mappings = [
            SlideTimecodeRef(slide_number=1, video_timecode_start="00:01:00"),
        ]

        result = step.merge([presentation], mappings=mappings)

        chunks = result.documents[0].chunks
        assert chunks[0].metadata["video_timecode"] == "00:01:00"
        assert "video_timecode" not in chunks[1].metadata

    def test_stable_sort_same_type(self) -> None:
        """Multiple documents of the same type preserve relative order."""
        step = MergeStep()
        video_a = _make_doc(SourceType.VIDEO)
        video_a = video_a.model_copy(update={"title": "Video A"})
        video_b = _make_doc(SourceType.VIDEO)
        video_b = video_b.model_copy(update={"title": "Video B"})
        text = _make_doc(SourceType.TEXT)

        result = step.merge([video_a, text, video_b])

        assert result.documents[0].title == "Video A"
        assert result.documents[1].title == "Video B"
        assert result.documents[2].source_type == SourceType.TEXT

    def test_created_at_set(self) -> None:
        """CourseContext has created_at timestamp."""
        step = MergeStep()

        result = step.merge([_make_doc()])

        assert result.created_at is not None
