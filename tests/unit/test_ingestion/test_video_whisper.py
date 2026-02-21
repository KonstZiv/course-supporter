"""Tests for WhisperVideoProcessor and VideoProcessor fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.heavy_steps import Transcript, TranscriptSegment
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


def _mock_transcribe_func(
    segments: list[TranscriptSegment] | None = None,
) -> AsyncMock:
    """Create a mock TranscribeFunc returning Transcript."""
    if segments is None:
        segments = [
            TranscriptSegment(start_sec=0.0, end_sec=10.0, text="Hello"),
            TranscriptSegment(start_sec=10.0, end_sec=20.0, text="World"),
        ]
    return AsyncMock(return_value=Transcript(segments=segments))


class TestWhisperVideoProcessor:
    async def test_success(self) -> None:
        """Injected transcribe_func -> SourceDocument with chunks."""
        mock_func = _mock_transcribe_func()
        proc = WhisperVideoProcessor(transcribe_func=mock_func)

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/audio.wav"),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        assert len(doc.chunks) == 2
        assert doc.metadata["strategy"] == "whisper"

    async def test_timecodes(self) -> None:
        """Segments carry start_sec/end_sec in metadata."""
        mock_func = _mock_transcribe_func(
            segments=[
                TranscriptSegment(start_sec=5.5, end_sec=15.3, text="speech"),
            ]
        )
        proc = WhisperVideoProcessor(transcribe_func=mock_func)

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.metadata["start_sec"] == 5.5
        assert chunk.metadata["end_sec"] == 15.3

    async def test_no_ffmpeg(self) -> None:
        """FFmpeg not found -> ProcessingError."""
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

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
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

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
        mock_func = _mock_transcribe_func(segments=[])
        proc = WhisperVideoProcessor(transcribe_func=mock_func)

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_invalid_source_type(self) -> None:
        """Non-video source -> UnsupportedFormatError."""
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process(_make_source(source_type="text"))

    async def test_cleanup_on_error(self) -> None:
        """Temp audio file cleaned up even when transcription fails."""
        mock_func = AsyncMock(side_effect=RuntimeError("Whisper crash"))
        proc = WhisperVideoProcessor(transcribe_func=mock_func)

        mock_unlink = MagicMock()
        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch("pathlib.Path.unlink", mock_unlink),
            pytest.raises(RuntimeError, match="Whisper crash"),
        ):
            await proc.process(_make_source())

        mock_unlink.assert_called_once()

    async def test_transcribe_func_called_with_params(self) -> None:
        """transcribe_func receives audio_path and TranscribeParams."""
        mock_func = _mock_transcribe_func()
        proc = WhisperVideoProcessor(transcribe_func=mock_func)

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/audio.wav"),
            patch("pathlib.Path.unlink"),
        ):
            await proc.process(_make_source())

        mock_func.assert_awaited_once()
        args = mock_func.call_args
        assert args[0][0] == "/tmp/audio.wav"


class TestWhisperUrlDownload:
    async def test_url_triggers_download(self) -> None:
        """HTTP URL triggers yt-dlp download before FFmpeg."""
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

        with (
            patch.object(
                proc, "_download_audio", return_value="/tmp/downloaded.wav"
            ) as mock_dl,
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
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

        with (
            patch.object(proc, "_download_audio") as mock_dl,
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
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

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
        proc = WhisperVideoProcessor(transcribe_func=_mock_transcribe_func())

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

    def test_transcribe_func_passed_to_whisper(self) -> None:
        """transcribe_func is forwarded to WhisperVideoProcessor."""
        mock_func = AsyncMock()
        proc = VideoProcessor(enable_whisper=True, transcribe_func=mock_func)
        assert isinstance(proc._whisper, WhisperVideoProcessor)
        assert proc._whisper._transcribe_func is mock_func  # type: ignore[union-attr]


class TestWhisperVideoProcessorDefaults:
    def test_default_transcribe_func_is_local_transcribe(self) -> None:
        """WhisperVideoProcessor() without args uses local_transcribe."""
        from course_supporter.ingestion.transcribe import local_transcribe

        proc = WhisperVideoProcessor()
        assert proc._transcribe_func is local_transcribe

    def test_injected_func_used_instead_of_default(self) -> None:
        """Explicit transcribe_func overrides the default."""
        custom = AsyncMock()
        proc = WhisperVideoProcessor(transcribe_func=custom)
        assert proc._transcribe_func is custom
