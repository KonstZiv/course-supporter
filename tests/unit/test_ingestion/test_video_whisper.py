"""Tests for WhisperVideoProcessor and VideoProcessor fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.heavy_steps import Transcript, TranscriptSegment
from course_supporter.ingestion.video import (
    VideoProcessor,
    WhisperVideoProcessor,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


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
        assert chunk.start_sec == 5.5
        assert chunk.end_sec == 15.3

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


class TestVideoProcessorParallel:
    """Test parallel STT + VD in VideoProcessor."""

    async def test_stt_plus_vd_parallel(self) -> None:
        """Both STT and VD produce chunks in one SourceDocument."""
        mock_stt = AsyncMock()
        mock_stt.process.return_value = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url="file:///v.mp4",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text="Hello world",
                    start_sec=0.0,
                    end_sec=10.0,
                ),
            ],
        )

        mock_vd = AsyncMock()
        from course_supporter.vd.schemas import (
            EyesResult,
            Scene,
            SceneAnalysis,
            SceneMemory,
            VDResult,
            VideoMemory,
        )

        mock_vd.process.return_value = VDResult(
            scenes=[
                SceneAnalysis(
                    scene=Scene(
                        scene_id=0,
                        frame_ids=["f0"],
                        start_sec=0.0,
                        end_sec=10.0,
                    ),
                    eyes_results=[
                        EyesResult(
                            frame_id="f0",
                            timestamp_sec=0.0,
                            scene_id=0,
                            response="test",
                            n_images=1,
                            latency_sec=1.0,
                            input_tokens=100,
                            output_tokens=50,
                        ),
                    ],
                    scene_memory=SceneMemory(
                        scene_id=0,
                        summary="Code editor with Python",
                        scene_type="screen_recording",
                        topics=["python"],
                        importance=4,
                    ),
                ),
            ],
            video_memory=VideoMemory(text="Python tutorial"),
            frames_total=1,
            frames_analyzed=1,
            model="test",
        )

        proc = VideoProcessor(stt=mock_stt, vd_pipeline=mock_vd)
        doc = await proc.process(_make_source())

        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.TRANSCRIPT
        assert doc.chunks[1].chunk_type == ChunkType.VISUAL_SCENE
        assert doc.chunks[1].text == "Code editor with Python"
        assert doc.chunks[1].start_sec == 0.0
        assert doc.chunks[1].metadata["scene_type"] == "screen_recording"
        assert doc.metadata["strategy"] == "stt+vd"

    async def test_vd_failure_graceful_degradation(self) -> None:
        """VD failure returns STT-only result."""
        mock_stt = AsyncMock()
        mock_stt.process.return_value = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url="file:///v.mp4",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text="Speech",
                    start_sec=0.0,
                    end_sec=5.0,
                ),
            ],
        )

        mock_vd = AsyncMock()
        mock_vd.process.side_effect = RuntimeError("VD crashed")

        proc = VideoProcessor(stt=mock_stt, vd_pipeline=mock_vd)
        doc = await proc.process(_make_source())

        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.TRANSCRIPT
        assert doc.metadata["strategy"] == "stt"

    async def test_no_vd_pipeline(self) -> None:
        """Without VD pipeline, STT-only result."""
        mock_stt = AsyncMock()
        mock_stt.process.return_value = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url="file:///v.mp4",
            chunks=[],
        )

        proc = VideoProcessor(stt=mock_stt, vd_pipeline=None)
        doc = await proc.process(_make_source())

        assert doc.metadata["strategy"] == "stt"
        assert doc.metadata["vd_scenes"] == 0


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
