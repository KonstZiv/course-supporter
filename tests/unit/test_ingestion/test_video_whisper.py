"""Tests for WhisperVideoProcessor and VideoProcessor fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video import (
    VideoProcessor,
    WhisperVideoProcessor,
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


class TestWhisperVideoProcessor:
    async def test_success(self) -> None:
        """Mocked Whisper transcribe -> SourceDocument with chunks."""
        proc = WhisperVideoProcessor()

        mock_segments = [
            {"start": 0.0, "end": 10.0, "text": "Hello"},
            {"start": 10.0, "end": 20.0, "text": "World"},
        ]

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/audio.wav"),
            patch.object(proc, "_transcribe", return_value=mock_segments),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        assert len(doc.chunks) == 2
        assert doc.metadata["strategy"] == "whisper"

    async def test_timecodes(self) -> None:
        """Segments carry start_sec/end_sec in metadata."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch.object(
                proc,
                "_transcribe",
                return_value=[{"start": 5.5, "end": 15.3, "text": "speech"}],
            ),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.metadata["start_sec"] == 5.5
        assert chunk.metadata["end_sec"] == 15.3

    async def test_no_ffmpeg(self) -> None:
        """FFmpeg not found -> ProcessingError."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(
                proc,
                "_extract_audio",
                side_effect=ProcessingError("FFmpeg not found"),
            ),
            pytest.raises(ProcessingError, match="FFmpeg not found"),
        ):
            await proc.process(_make_source())

    async def test_ffmpeg_fails(self) -> None:
        """FFmpeg subprocess error -> ProcessingError."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(
                proc,
                "_extract_audio",
                side_effect=ProcessingError("FFmpeg failed (code 1)"),
            ),
            pytest.raises(ProcessingError, match="FFmpeg failed"),
        ):
            await proc.process(_make_source())

    async def test_empty_audio(self) -> None:
        """Empty transcription result -> empty chunks."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch.object(proc, "_transcribe", return_value=[]),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_invalid_source_type(self) -> None:
        """Non-video source -> UnsupportedFormatError."""
        proc = WhisperVideoProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process(_make_source(source_type="text"))

    async def test_cleanup_on_error(self) -> None:
        """Temp audio file cleaned up even when transcription fails."""
        proc = WhisperVideoProcessor()

        mock_unlink = MagicMock()
        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch.object(
                proc, "_transcribe", side_effect=RuntimeError("Whisper crash")
            ),
            patch("pathlib.Path.unlink", mock_unlink),
            pytest.raises(RuntimeError, match="Whisper crash"),
        ):
            await proc.process(_make_source())

        mock_unlink.assert_called_once()


class TestWhisperUrlDownload:
    async def test_url_triggers_download(self) -> None:
        """HTTP URL triggers yt-dlp download before FFmpeg."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(
                proc, "_download_audio", return_value="/tmp/downloaded.wav"
            ) as mock_dl,
            patch.object(proc, "_transcribe", return_value=[]),
            patch("pathlib.Path.unlink"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=AsyncMock(
                    communicate=AsyncMock(return_value=(b"", b"")),
                    returncode=0,
                ),
            ),
        ):
            await proc.process(
                _make_source(url="https://www.youtube.com/watch?v=abc123")
            )

        mock_dl.assert_awaited_once_with("https://www.youtube.com/watch?v=abc123")

    async def test_local_path_skips_download(self) -> None:
        """Local file path does not trigger yt-dlp download."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(proc, "_download_audio") as mock_dl,
            patch.object(proc, "_transcribe", return_value=[]),
            patch("pathlib.Path.unlink"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=AsyncMock(
                    communicate=AsyncMock(return_value=(b"", b"")),
                    returncode=0,
                ),
            ),
        ):
            await proc.process(_make_source(url="/tmp/local_video.mp4"))

        mock_dl.assert_not_awaited()

    async def test_download_ytdlp_not_found(self) -> None:
        """yt-dlp not installed -> ProcessingError."""
        proc = WhisperVideoProcessor()

        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(ProcessingError, match="yt-dlp not found"),
        ):
            await proc._download_audio("https://youtube.com/watch?v=test")

    async def test_download_ytdlp_fails(self) -> None:
        """yt-dlp returns non-zero exit code -> ProcessingError."""
        proc = WhisperVideoProcessor()

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=AsyncMock(
                    communicate=AsyncMock(
                        return_value=(b"", b"ERROR: Video unavailable")
                    ),
                    returncode=1,
                ),
            ),
            pytest.raises(ProcessingError, match="yt-dlp failed"),
        ):
            await proc._download_audio("https://youtube.com/watch?v=test")


class TestVideoProcessorFallbackOnLowTokens:
    async def test_low_tokens_triggers_whisper_fallback(self) -> None:
        """Gemini returns low tokens_in -> fallback to Whisper."""
        proc = VideoProcessor(enable_whisper=False)

        # Mock Whisper processor
        mock_whisper = AsyncMock()
        mock_whisper.process.return_value = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url="https://youtube.com/watch?v=long",
            metadata={"strategy": "whisper"},
        )
        proc._whisper = mock_whisper

        # Gemini returns low tokens (video not seen)
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:10] Hallucinated",
            tokens_in=50,
        )

        doc = await proc.process(
            _make_source(url="https://youtube.com/watch?v=long"),
            router=router,
        )
        assert doc.metadata["strategy"] == "whisper"
        mock_whisper.process.assert_awaited_once()


class TestVideoProcessorFallback:
    async def test_fallback_to_whisper(self) -> None:
        """Gemini fails -> Whisper succeeds."""
        proc = VideoProcessor(enable_whisper=False)
        mock_whisper = AsyncMock()
        mock_whisper.process.return_value = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url="file:///v.mp4",
            metadata={"strategy": "whisper"},
        )
        proc._whisper = mock_whisper

        proc._gemini = AsyncMock()
        proc._gemini.process.side_effect = RuntimeError("Gemini down")

        doc = await proc.process(_make_source())
        assert doc.metadata["strategy"] == "whisper"

    async def test_both_fail(self) -> None:
        """Both Gemini and Whisper fail -> raise last error."""
        proc = VideoProcessor(enable_whisper=False)
        mock_whisper = AsyncMock()
        mock_whisper.process.side_effect = ProcessingError("Whisper also failed")
        proc._whisper = mock_whisper

        proc._gemini = AsyncMock()
        proc._gemini.process.side_effect = RuntimeError("Gemini down")

        with pytest.raises(ProcessingError, match="Whisper also failed"):
            await proc.process(_make_source())

    def test_enable_whisper_true(self) -> None:
        """enable_whisper=True creates WhisperVideoProcessor."""
        proc = VideoProcessor(enable_whisper=True)
        assert isinstance(proc._whisper, WhisperVideoProcessor)

    def test_enable_whisper_false(self) -> None:
        """enable_whisper=False leaves _whisper as None."""
        proc = VideoProcessor(enable_whisper=False)
        assert proc._whisper is None
