"""Tests for GeminiVideoProcessor and VideoProcessor."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video import (
    GeminiVideoProcessor,
    VideoProcessor,
    _timecode_to_seconds,
)
from course_supporter.models.source import ChunkType, SourceDocument, SourceType


def _make_source(
    source_type: str = "video",
    url: str = "file:///v.mp4",
    filename: str = "v.mp4",
) -> MagicMock:
    """Create a mock SourceMaterial."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


class TestGeminiVideoProcessor:
    async def test_success(self) -> None:
        """Mocked router.complete returns valid transcript -> SourceDocument."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:30] Hello world\n[0:30-1:00] More text"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        assert len(doc.chunks) == 2

    async def test_parses_timecodes(self) -> None:
        """Transcript timestamps parsed into start_sec/end_sec metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(content="[1:30-2:00] Some speech")

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.metadata["start_sec"] == 90.0
        assert chunk.metadata["end_sec"] == 120.0

    async def test_requires_router(self) -> None:
        """None router raises ProcessingError."""
        proc = GeminiVideoProcessor()
        with pytest.raises(ProcessingError, match="requires a ModelRouter"):
            await proc.process(_make_source(), router=None)

    async def test_invalid_source_type(self) -> None:
        """Non-video source raises UnsupportedFormatError."""
        proc = GeminiVideoProcessor()
        router = AsyncMock()
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process(_make_source(source_type="text"), router=router)

    async def test_llm_failure_propagates(self) -> None:
        """Router exception propagates as-is."""
        router = AsyncMock()
        router.complete.side_effect = RuntimeError("API down")

        proc = GeminiVideoProcessor()
        with pytest.raises(RuntimeError, match="API down"):
            await proc.process(_make_source(), router=router)

    async def test_output_fields(self) -> None:
        """Verify output shape: source_type, source_url, metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(content="[0:00-0:10] Hi")

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(url="s3://bucket/v.mp4"), router=router)

        assert doc.source_url == "s3://bucket/v.mp4"
        assert doc.metadata["strategy"] == "gemini"

    async def test_non_timestamped_lines(self) -> None:
        """Lines without timecodes become plain TRANSCRIPT chunks."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:15] First segment\nSome note without timestamp"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert len(doc.chunks) == 2
        assert doc.chunks[0].metadata["start_sec"] == 0.0
        assert doc.chunks[1].metadata == {}

    async def test_empty_and_whitespace_lines_skipped(self) -> None:
        """Empty lines and whitespace-only lines produce no chunks."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] First\n\n   \n[0:10-0:20] Second"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert len(doc.chunks) == 2
        assert doc.chunks[0].text == "First"
        assert doc.chunks[1].text == "Second"
        assert doc.chunks[0].index == 0
        assert doc.chunks[1].index == 1

    async def test_chunk_ordering(self) -> None:
        """Chunks indexed sequentially."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] A\n[0:10-0:20] B\n[0:20-0:30] C"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        indices = [c.index for c in doc.chunks]
        assert indices == [0, 1, 2]


class TestVideoProcessor:
    async def test_delegates_to_gemini(self) -> None:
        """VideoProcessor delegates to GeminiVideoProcessor."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(content="[0:00-0:05] Test")

        proc = VideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO

    async def test_unsupported_format_not_caught(self) -> None:
        """UnsupportedFormatError propagates without fallback attempt."""
        router = AsyncMock()

        proc = VideoProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process(_make_source(source_type="text"), router=router)

    async def test_fallback_no_whisper_re_raises(self) -> None:
        """Re-raises if whisper is None and Gemini fails."""
        router = AsyncMock()
        router.complete.side_effect = RuntimeError("Gemini down")

        proc = VideoProcessor()
        assert proc._whisper is None

        with pytest.raises(RuntimeError, match="Gemini down"):
            await proc.process(_make_source(), router=router)


class TestTimecodeToSeconds:
    def test_mm_ss(self) -> None:
        """MM:SS format converts correctly."""
        assert _timecode_to_seconds("1:30") == 90.0

    def test_h_mm_ss(self) -> None:
        """H:MM:SS format converts correctly."""
        assert _timecode_to_seconds("1:02:30") == 3750.0

    def test_zero(self) -> None:
        """0:00 -> 0.0."""
        assert _timecode_to_seconds("0:00") == 0.0

    def test_hh_mm_ss(self) -> None:
        """HH:MM:SS with two-digit hours converts correctly."""
        assert _timecode_to_seconds("12:00:00") == 43200.0

    def test_invalid_format(self) -> None:
        """Invalid format returns 0.0."""
        assert _timecode_to_seconds("invalid") == 0.0
