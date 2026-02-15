# 📋 S1-013: VideoProcessor (Fallback — FFmpeg + Whisper)

## Мета

Додати fallback-стратегію для відеообробки: FFmpeg витягує аудіо-доріжку → OpenAI Whisper транскрибує локально → `SourceDocument`. Підключити `WhisperVideoProcessor` до `VideoProcessor` як автоматичний fallback при збої Gemini.

## Контекст

Залежить від S1-011 (schemas), S1-012 (GeminiVideoProcessor + VideoProcessor shell). Whisper — optional dependency (`uv sync --extra media`). FFmpeg — system dependency, не Python-пакет. У CI тести мокають subprocess та whisper.

---

## Acceptance Criteria

- [ ] `WhisperVideoProcessor` реалізує `SourceProcessor.process()`
- [ ] FFmpeg subprocess витягує аудіо з відео
- [ ] Whisper транскрибує аудіо → сегменти з таймкодами
- [ ] Сегменти → `ContentChunk(chunk_type=TRANSCRIPT)` з `start_sec`/`end_sec`
- [ ] FFmpeg not found → `ProcessingError`
- [ ] FFmpeg subprocess error → `ProcessingError`
- [ ] `VideoProcessor` updated: Gemini fails → Whisper fallback
- [ ] Обидва fail → raise останню помилку
- [ ] ~7 unit-тестів з мокнутим FFmpeg + Whisper
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/video.py (доповнення)

```python
import asyncio
import tempfile
from pathlib import Path

import structlog

from course_supporter.ingestion.base import (
    ProcessingError,
    SourceProcessor,
    UnsupportedFormatError,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
)

logger = structlog.get_logger()


class WhisperVideoProcessor(SourceProcessor):
    """Process video using local FFmpeg + OpenAI Whisper.

    Extracts audio track via FFmpeg subprocess, then transcribes
    using whisper library. No external API calls required.

    Requires:
    - FFmpeg installed on the system
    - whisper package (optional 'media' dependency group)
    """

    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        if source.source_type != "video":
            raise UnsupportedFormatError(
                f"WhisperVideoProcessor expects 'video', got '{source.source_type}'"
            )

        logger.info(
            "whisper_video_processing_start",
            source_url=source.source_url,
        )

        # 1. Extract audio from video
        audio_path = await self._extract_audio(source.source_url)

        try:
            # 2. Transcribe audio with Whisper
            segments = await self._transcribe(audio_path)

            # 3. Convert segments to chunks
            chunks = self._segments_to_chunks(segments)

            logger.info(
                "whisper_video_processing_done",
                source_url=source.source_url,
                chunk_count=len(chunks),
            )

            return SourceDocument(
                source_type="video",
                source_url=source.source_url,
                title=source.filename or "",
                chunks=chunks,
                metadata={"strategy": "whisper"},
            )
        finally:
            # Clean up temp audio file
            Path(audio_path).unlink(missing_ok=True)

    async def _extract_audio(self, video_path: str) -> str:
        """Extract audio from video using FFmpeg.

        Args:
            video_path: Path to the video file.

        Returns:
            Path to the extracted WAV audio file.

        Raises:
            ProcessingError: If FFmpeg is not found or fails.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            audio_path = tmp.name

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                audio_path,
                "-y",  # overwrite
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                raise ProcessingError(
                    f"FFmpeg failed (code {process.returncode}): {stderr.decode()[:500]}"
                )

        except FileNotFoundError:
            raise ProcessingError(
                "FFmpeg not found. Install FFmpeg to use WhisperVideoProcessor."
            )

        return audio_path

    async def _transcribe(self, audio_path: str) -> list[dict]:
        """Transcribe audio file using Whisper.

        Runs Whisper in a thread pool to avoid blocking the event loop.

        Args:
            audio_path: Path to the WAV audio file.

        Returns:
            List of segment dicts with 'start', 'end', 'text' keys.
        """
        import whisper  # type: ignore[import-untyped]

        loop = asyncio.get_event_loop()
        model = await loop.run_in_executor(None, whisper.load_model, "base")
        result = await loop.run_in_executor(None, model.transcribe, audio_path)
        return result.get("segments", [])

    @staticmethod
    def _segments_to_chunks(segments: list[dict]) -> list[ContentChunk]:
        """Convert Whisper segments to ContentChunks."""
        chunks: list[ContentChunk] = []
        for idx, seg in enumerate(segments):
            text = seg.get("text", "").strip()
            if not text:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text=text,
                    index=idx,
                    metadata={
                        "start_sec": round(seg.get("start", 0.0), 2),
                        "end_sec": round(seg.get("end", 0.0), 2),
                    },
                )
            )
        return chunks
```

### VideoProcessor update (в тому ж файлі)

```python
class VideoProcessor(SourceProcessor):
    """Composite video processor with Gemini primary + Whisper fallback."""

    def __init__(self, *, enable_whisper: bool = True) -> None:
        self._gemini = GeminiVideoProcessor()
        self._whisper: SourceProcessor | None = (
            WhisperVideoProcessor() if enable_whisper else None
        )

    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        try:
            return await self._gemini.process(source, router=router)
        except Exception:
            if self._whisper is not None:
                logger.warning(
                    "gemini_video_failed_trying_whisper",
                    source_url=source.source_url,
                )
                return await self._whisper.process(source)
            raise
```

---

## Тести

### tests/unit/test_ingestion/test_video_whisper.py

```python
"""Tests for WhisperVideoProcessor and VideoProcessor fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video import (
    VideoProcessor,
    WhisperVideoProcessor,
)
from course_supporter.models.source import ChunkType, SourceDocument


def _make_source(source_type: str = "video", url: str = "file:///v.mp4") -> MagicMock:
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = "v.mp4"
    return source


class TestWhisperVideoProcessor:
    async def test_whisper_processor_success(self) -> None:
        """Mocked Whisper transcribe → SourceDocument with chunks."""
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
        assert len(doc.chunks) == 2
        assert doc.metadata["strategy"] == "whisper"

    async def test_whisper_processor_timecodes(self) -> None:
        """Segments carry start_sec/end_sec in metadata."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch.object(
                proc, "_transcribe",
                return_value=[{"start": 5.5, "end": 15.3, "text": "speech"}],
            ),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.metadata["start_sec"] == 5.5
        assert chunk.metadata["end_sec"] == 15.3

    async def test_whisper_processor_no_ffmpeg(self) -> None:
        """FFmpeg not found → ProcessingError."""
        proc = WhisperVideoProcessor()

        with patch.object(
            proc, "_extract_audio", side_effect=ProcessingError("FFmpeg not found")
        ):
            with pytest.raises(ProcessingError, match="FFmpeg not found"):
                await proc.process(_make_source())

    async def test_whisper_processor_ffmpeg_fails(self) -> None:
        """FFmpeg subprocess error → ProcessingError."""
        proc = WhisperVideoProcessor()

        with patch.object(
            proc, "_extract_audio",
            side_effect=ProcessingError("FFmpeg failed (code 1)"),
        ):
            with pytest.raises(ProcessingError, match="FFmpeg failed"):
                await proc.process(_make_source())

    async def test_whisper_processor_empty_audio(self) -> None:
        """Empty transcription result → empty chunks."""
        proc = WhisperVideoProcessor()

        with (
            patch.object(proc, "_extract_audio", return_value="/tmp/a.wav"),
            patch.object(proc, "_transcribe", return_value=[]),
            patch("pathlib.Path.unlink"),
        ):
            doc = await proc.process(_make_source())

        assert doc.chunks == []


class TestVideoProcessorFallback:
    async def test_video_processor_fallback_to_whisper(self) -> None:
        """Gemini fails → Whisper succeeds."""
        proc = VideoProcessor(enable_whisper=False)
        # Manually set a mock whisper
        mock_whisper = AsyncMock()
        mock_whisper.process.return_value = SourceDocument(
            source_type="video",
            source_url="file:///v.mp4",
            metadata={"strategy": "whisper"},
        )
        proc._whisper = mock_whisper

        # Make Gemini fail
        proc._gemini = AsyncMock()
        proc._gemini.process.side_effect = RuntimeError("Gemini down")

        doc = await proc.process(_make_source())
        assert doc.metadata["strategy"] == "whisper"

    async def test_video_processor_both_fail(self) -> None:
        """Both Gemini and Whisper fail → raise last error."""
        proc = VideoProcessor(enable_whisper=False)
        mock_whisper = AsyncMock()
        mock_whisper.process.side_effect = ProcessingError("Whisper also failed")
        proc._whisper = mock_whisper

        proc._gemini = AsyncMock()
        proc._gemini.process.side_effect = RuntimeError("Gemini down")

        with pytest.raises(ProcessingError, match="Whisper also failed"):
            await proc.process(_make_source())
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── video.py                 # GeminiVideoProcessor + WhisperVideoProcessor + VideoProcessor

tests/unit/test_ingestion/
├── test_video.py            # S1-012 tests
└── test_video_whisper.py    # ~7 tests
```

---

## Кроки виконання

1. Переконатися, що S1-012 завершено (GeminiVideoProcessor + VideoProcessor shell)
2. Додати `WhisperVideoProcessor` до `ingestion/video.py`
3. Оновити `VideoProcessor.__init__` — підключити WhisperVideoProcessor з `enable_whisper` flag
4. Створити `tests/unit/test_ingestion/test_video_whisper.py`
5. `make check`

---

## Примітки

- **FFmpeg як system dependency**: не встановлюється через pip/uv. У Docker — `apt-get install ffmpeg`. У CI — мокаємо subprocess.
- **Whisper як optional dependency**: `uv sync --extra media` (PyTorch ~2GB). Імпортується всередині методу `_transcribe()` — lazy import.
- **Thread pool для Whisper**: `whisper.load_model()` та `model.transcribe()` — blocking CPU-intensive операції, виконуються через `loop.run_in_executor(None, ...)`.
- **Temp file cleanup**: `finally` блок гарантує видалення тимчасового WAV файлу.
- **enable_whisper=True**: за замовчуванням fallback увімкнено. У тестах S1-012 — `enable_whisper=False` щоб тестувати тільки Gemini.
