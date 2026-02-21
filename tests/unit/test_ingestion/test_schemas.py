"""Tests for ingestion pipeline schemas and interfaces."""

from datetime import datetime

import pytest

from course_supporter.ingestion.base import (
    ProcessingError,
    SourceProcessor,
    UnsupportedFormatError,
)
from course_supporter.models.course import CourseContext, SlideTimecodeRef
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


class TestSourceType:
    def test_source_type_values(self) -> None:
        """All expected source types exist with correct string values."""
        assert SourceType.VIDEO == "video"
        assert SourceType.PRESENTATION == "presentation"
        assert SourceType.TEXT == "text"
        assert SourceType.WEB == "web"

    def test_source_type_matches_orm_enum(self) -> None:
        """SourceType values match ORM source_type_enum."""
        assert len(SourceType) == 4


class TestChunkType:
    def test_chunk_type_values(self) -> None:
        """All expected chunk types exist with correct string values."""
        assert ChunkType.TRANSCRIPT == "transcript"
        assert ChunkType.SLIDE_TEXT == "slide_text"
        assert ChunkType.SLIDE_DESCRIPTION == "slide_description"
        assert ChunkType.PARAGRAPH == "paragraph"
        assert ChunkType.HEADING == "heading"
        assert ChunkType.WEB_CONTENT == "web_content"
        assert ChunkType.METADATA == "metadata"


class TestContentChunk:
    def test_content_chunk_default_metadata(self) -> None:
        """ContentChunk metadata defaults to empty dict."""
        chunk = ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="hello")
        assert chunk.metadata == {}
        assert chunk.index == 0

    def test_content_chunk_with_timecodes(self) -> None:
        """Transcript chunk carries start/end timecodes in metadata."""
        chunk = ContentChunk(
            chunk_type=ChunkType.TRANSCRIPT,
            text="Hello world",
            index=0,
            metadata={"start_sec": 0.0, "end_sec": 30.0},
        )
        assert chunk.metadata["start_sec"] == 0.0
        assert chunk.metadata["end_sec"] == 30.0


class TestSourceDocument:
    def test_source_document_defaults(self) -> None:
        """SourceDocument has empty chunks and auto processed_at."""
        doc = SourceDocument(source_type=SourceType.TEXT, source_url="file:///test.md")
        assert doc.chunks == []
        assert doc.title == ""
        assert isinstance(doc.processed_at, datetime)
        assert doc.metadata == {}

    def test_source_document_with_chunks(self) -> None:
        """SourceDocument holds multiple content chunks."""
        chunks = [
            ContentChunk(chunk_type=ChunkType.HEADING, text="Title", index=0),
            ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="Body", index=1),
        ]
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///test.md",
            title="My Doc",
            chunks=chunks,
        )
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.HEADING


class TestCourseContext:
    def test_course_context_empty(self) -> None:
        """CourseContext with no documents."""
        ctx = CourseContext(documents=[])
        assert ctx.documents == []
        assert ctx.slide_video_mappings == []
        assert isinstance(ctx.created_at, datetime)

    def test_course_context_with_mappings(self) -> None:
        """CourseContext with documents and slide-video mappings."""
        doc = SourceDocument(source_type=SourceType.VIDEO, source_url="file:///v.mp4")
        mapping = SlideTimecodeRef(slide_number=1, video_timecode_start="00:05:30")
        ctx = CourseContext(
            documents=[doc],
            slide_video_mappings=[mapping],
        )
        assert len(ctx.documents) == 1
        assert ctx.slide_video_mappings[0].slide_number == 1
        assert ctx.slide_video_mappings[0].video_timecode_start == "00:05:30"


class TestSourceProcessor:
    def test_source_processor_is_abstract(self) -> None:
        """SourceProcessor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SourceProcessor()  # type: ignore[abstract]

    def test_processing_error_hierarchy(self) -> None:
        """UnsupportedFormatError is a subclass of ProcessingError."""
        assert issubclass(UnsupportedFormatError, ProcessingError)
        assert issubclass(ProcessingError, Exception)
