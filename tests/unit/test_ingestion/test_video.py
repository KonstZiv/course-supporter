"""Tests for GeminiVideoProcessor and VideoProcessor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video import (
    GeminiVideoProcessor,
    VideoProcessor,
    _timecode_to_seconds,
)
from course_supporter.models.source import (
    ChunkType,
    SourceDocument,
    SourceType,
)
from course_supporter.stt.schemas import STTResult, STTSegment


def _make_source(
    source_type: str = "video",
    url: str = "file:///v.mp4",
    filename: str = "v.mp4",
) -> MagicMock:
    """Create a mock MaterialEntry."""
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
            content="[0:00-0:30] Hello world\n[0:30-1:00] More text",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(_make_source(), router=router)

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        assert len(doc.chunks) == 2

    async def test_parses_timecodes(self) -> None:
        """Transcript timestamps parsed into start_sec/end_sec metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[1:30-2:00] Some speech",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(_make_source(), router=router)

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.start_sec == 90.0
        assert chunk.end_sec == 120.0

    async def test_requires_router(self) -> None:
        """None router raises ProcessingError."""
        proc = GeminiVideoProcessor()
        with pytest.raises(ProcessingError, match="requires a ModelRouter"):
            await proc.process_raw(_make_source(), router=None)

    async def test_invalid_source_type(self) -> None:
        """Non-video source raises UnsupportedFormatError."""
        proc = GeminiVideoProcessor()
        router = AsyncMock()
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process_raw(_make_source(source_type="text"), router=router)

    async def test_llm_failure_propagates(self) -> None:
        """Router exception propagates as-is."""
        router = AsyncMock()
        router.complete.side_effect = RuntimeError("API down")

        proc = GeminiVideoProcessor()
        with pytest.raises(RuntimeError, match="API down"):
            await proc.process_raw(_make_source(), router=router)

    async def test_output_fields(self) -> None:
        """Verify output shape: source_type, source_url, metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] Hi",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(
            _make_source(url="s3://bucket/v.mp4"), router=router
        )

        assert doc.source_url == "s3://bucket/v.mp4"
        assert doc.metadata["strategy"] == "gemini"

    async def test_non_timestamped_lines(self) -> None:
        """Lines without timecodes become plain TRANSCRIPT chunks."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:15] First segment\nSome note without timestamp",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(_make_source(), router=router)

        assert len(doc.chunks) == 2
        assert doc.chunks[0].start_sec == 0.0
        assert doc.chunks[1].start_sec is None

    async def test_empty_and_whitespace_lines_skipped(self) -> None:
        """Empty lines and whitespace-only lines produce no chunks."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] First\n\n   \n[0:10-0:20] Second",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(_make_source(), router=router)

        assert len(doc.chunks) == 2
        assert doc.chunks[0].text == "First"
        assert doc.chunks[1].text == "Second"
        assert doc.chunks[0].index == 0
        assert doc.chunks[1].index == 1

    async def test_chunk_ordering(self) -> None:
        """Chunks indexed sequentially."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] A\n[0:10-0:20] B\n[0:20-0:30] C",
            tokens_in=50000,
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process_raw(_make_source(), router=router)

        indices = [c.index for c in doc.chunks]
        assert indices == [0, 1, 2]

    async def test_low_tokens_raises_processing_error(self) -> None:
        """tokens_in below threshold -> ProcessingError (video not seen)."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] Hallucinated text",
            tokens_in=89,
        )

        proc = GeminiVideoProcessor()
        with pytest.raises(ProcessingError, match="did not process the video"):
            await proc.process_raw(_make_source(), router=router)

    async def test_none_tokens_raises_processing_error(self) -> None:
        """tokens_in=None -> ProcessingError."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] Some text",
            tokens_in=None,
        )

        proc = GeminiVideoProcessor()
        with pytest.raises(ProcessingError, match="did not process the video"):
            await proc.process_raw(_make_source(), router=router)


def _mock_stt_router(
    segments: list[STTSegment] | None = None,
) -> AsyncMock:
    """Create a mock STTRouter returning STTResult."""
    if segments is None:
        segments = [
            STTSegment(start_sec=0.0, end_sec=10.0, text="Hello"),
            STTSegment(start_sec=10.0, end_sec=20.0, text="World"),
        ]
    router = AsyncMock()
    router.transcribe.return_value = STTResult(
        text=" ".join(s.text for s in segments),
        segments=segments,
        provider="mock",
        model_id="mock-model",
    )
    return router


class TestVideoProcessor:
    async def test_stt_only_when_no_vd(self) -> None:
        """VideoProcessor returns STT chunks when no VD pipeline."""
        mock_router = _mock_stt_router(
            [STTSegment(start_sec=0.0, end_sec=5.0, text="Hello")]
        )
        proc = VideoProcessor(stt_router=mock_router)

        with (
            patch(
                "course_supporter.ingestion.video._extract_audio_ffmpeg",
                return_value="/tmp/audio.wav",
            ),
            patch("pathlib.Path.unlink"),
            patch("pathlib.Path.is_file", return_value=True),
        ):
            doc = await proc.process_raw(_make_source())

        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.TRANSCRIPT
        assert doc.metadata["strategy"] == "stt"

    async def test_unsupported_format(self) -> None:
        """UnsupportedFormatError for non-video source."""
        proc = VideoProcessor(stt_router=AsyncMock())
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process_raw(_make_source(source_type="text"))

    async def test_url_source_rejected(self) -> None:
        """Remote URL raises ProcessingError (local files only)."""
        proc = VideoProcessor(stt_router=AsyncMock())
        with pytest.raises(ProcessingError, match="requires a local file path"):
            await proc.process_raw(_make_source(url="https://example.com/v.mp4"))


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
