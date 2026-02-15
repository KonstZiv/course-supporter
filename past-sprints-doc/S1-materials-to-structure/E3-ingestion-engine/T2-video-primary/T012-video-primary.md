# 📋 S1-012: VideoProcessor (Primary — Gemini Vision)

## Мета

Реалізувати основний відеопроцесор, що використовує Gemini Vision API для транскрипції відео з таймкодами. `GeminiVideoProcessor` завантажує відео через Gemini File API, отримує transcript, парсить у `ContentChunk` об'єкти. `VideoProcessor` — composition shell, який делегує до Gemini і готовий до fallback (S1-013).

## Контекст

Залежить від S1-011 (SourceProcessor ABC, ContentChunk, SourceDocument). Використовує `ModelRouter` з Epic 2 для виклику LLM. Action `video_analysis` вже зареєстрований в `config/models.yaml`.

---

## Acceptance Criteria

- [ ] `GeminiVideoProcessor` реалізує `SourceProcessor.process()`
- [ ] Завантаження відео через Gemini File API (google-genai SDK)
- [ ] Транскрипція з таймкодами через `router.complete(action="video_analysis")`
- [ ] Парсинг відповіді у `ContentChunk(chunk_type=TRANSCRIPT)` з metadata `start_sec`/`end_sec`
- [ ] Валідація `source_type == "video"` → `UnsupportedFormatError` якщо ні
- [ ] `router=None` → `ProcessingError`
- [ ] `VideoProcessor` shell з composition pattern (Gemini + placeholder для Whisper)
- [ ] ~8 unit-тестів з мокнутим router
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/video.py

```python
"""Video processor implementations.

Two strategies:
- GeminiVideoProcessor: Upload video to Gemini File API → vision transcript
- WhisperVideoProcessor: FFmpeg audio extraction → Whisper (S1-013)

VideoProcessor composes both with automatic fallback.
"""

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

# System prompt for video transcription via Gemini Vision
TRANSCRIPT_PROMPT = """\
Transcribe this video with precise timestamps.
Output format — one segment per line:
[MM:SS-MM:SS] transcribed text here

Rules:
- Include ALL spoken content
- Timestamps must be accurate to the second
- Each segment should be 15-30 seconds
- Preserve technical terminology exactly
- If there are on-screen text or diagrams, describe them in [VISUAL: ...]
"""


class GeminiVideoProcessor(SourceProcessor):
    """Process video using Gemini Vision API.

    Uploads video to Gemini File API, sends to vision model
    for transcription with timestamps, parses response into
    ContentChunks.
    """

    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        if source.source_type != "video":
            raise UnsupportedFormatError(
                f"GeminiVideoProcessor expects 'video', got '{source.source_type}'"
            )

        if router is None:
            raise ProcessingError(
                "GeminiVideoProcessor requires a ModelRouter for Gemini API calls"
            )

        logger.info(
            "gemini_video_processing_start",
            source_url=source.source_url,
        )

        # 1. Upload video to Gemini File API
        video_file = await self._upload_video(source.source_url)

        # 2. Call LLM with video content
        response = await router.complete(
            action="video_analysis",
            prompt=f"Video file: {video_file}\n\n{TRANSCRIPT_PROMPT}",
        )

        # 3. Parse transcript into chunks
        chunks = self._parse_transcript(response.content)

        logger.info(
            "gemini_video_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type="video",
            source_url=source.source_url,
            title=source.filename or "",
            chunks=chunks,
            metadata={"strategy": "gemini"},
        )

    async def _upload_video(self, source_url: str) -> str:
        """Upload video file to Gemini File API.

        Args:
            source_url: Path or URL to the video file.

        Returns:
            Gemini file reference string for use in prompts.
        """
        # TODO: Implement actual Gemini File API upload
        # google.genai.Client().files.upload(path=source_url)
        # Returns a File object with .uri for use in prompts
        return source_url

    def _parse_transcript(self, raw_text: str) -> list[ContentChunk]:
        """Parse timestamped transcript into ContentChunks.

        Expected format per line: [MM:SS-MM:SS] text content

        Args:
            raw_text: Raw transcript text from Gemini.

        Returns:
            List of ContentChunks with TRANSCRIPT type and timecode metadata.
        """
        import re

        chunks: list[ContentChunk] = []
        # Pattern: [MM:SS-MM:SS] or [H:MM:SS-H:MM:SS]
        pattern = re.compile(
            r"\[(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)"
        )

        for idx, line in enumerate(raw_text.strip().split("\n")):
            line = line.strip()
            if not line:
                continue

            match = pattern.match(line)
            if match:
                start_str, end_str, text = match.groups()
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.TRANSCRIPT,
                        text=text.strip(),
                        index=idx,
                        metadata={
                            "start_sec": self._timecode_to_seconds(start_str),
                            "end_sec": self._timecode_to_seconds(end_str),
                        },
                    )
                )
            else:
                # Non-timestamped line — include as plain transcript
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.TRANSCRIPT,
                        text=line,
                        index=idx,
                    )
                )

        return chunks

    @staticmethod
    def _timecode_to_seconds(timecode: str) -> float:
        """Convert timecode string to seconds.

        Supports MM:SS and H:MM:SS formats.
        """
        parts = timecode.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return 0.0


class VideoProcessor(SourceProcessor):
    """Composite video processor with Gemini primary + Whisper fallback.

    Tries GeminiVideoProcessor first. If it fails and Whisper is enabled,
    falls back to WhisperVideoProcessor (local FFmpeg + Whisper).
    """

    def __init__(self, *, enable_whisper: bool = False) -> None:
        self._gemini = GeminiVideoProcessor()
        # WhisperVideoProcessor added in S1-013
        self._whisper: SourceProcessor | None = None

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

### tests/unit/test_ingestion/test_video.py

```python
"""Tests for GeminiVideoProcessor and VideoProcessor."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video import GeminiVideoProcessor, VideoProcessor
from course_supporter.models.source import ChunkType, SourceDocument


def _make_source(source_type: str = "video", url: str = "file:///v.mp4") -> MagicMock:
    """Create a mock SourceMaterial."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = "v.mp4"
    return source


class TestGeminiVideoProcessor:
    async def test_gemini_processor_success(self) -> None:
        """Mocked router.complete returns valid transcript → SourceDocument."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[0:00-0:30] Hello world\n[0:30-1:00] More text"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == "video"
        assert len(doc.chunks) == 2

    async def test_gemini_processor_parses_timecodes(self) -> None:
        """Transcript timestamps parsed into start_sec/end_sec metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(
            content="[1:30-2:00] Some speech"
        )

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.TRANSCRIPT
        assert chunk.metadata["start_sec"] == 90.0
        assert chunk.metadata["end_sec"] == 120.0

    async def test_gemini_processor_requires_router(self) -> None:
        """None router raises ProcessingError."""
        proc = GeminiVideoProcessor()
        with pytest.raises(ProcessingError, match="requires a ModelRouter"):
            await proc.process(_make_source(), router=None)

    async def test_gemini_processor_invalid_source_type(self) -> None:
        """Non-video source raises UnsupportedFormatError."""
        proc = GeminiVideoProcessor()
        router = AsyncMock()
        with pytest.raises(UnsupportedFormatError, match="expects 'video'"):
            await proc.process(_make_source(source_type="text"), router=router)

    async def test_gemini_processor_llm_failure(self) -> None:
        """Router exception propagates as-is."""
        router = AsyncMock()
        router.complete.side_effect = RuntimeError("API down")

        proc = GeminiVideoProcessor()
        with pytest.raises(RuntimeError, match="API down"):
            await proc.process(_make_source(), router=router)

    async def test_source_document_has_correct_fields(self) -> None:
        """Verify output shape: source_type, source_url, metadata."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(content="[0:00-0:10] Hi")

        proc = GeminiVideoProcessor()
        doc = await proc.process(_make_source(url="s3://bucket/v.mp4"), router=router)

        assert doc.source_url == "s3://bucket/v.mp4"
        assert doc.metadata["strategy"] == "gemini"


class TestVideoProcessor:
    async def test_video_processor_uses_gemini(self) -> None:
        """VideoProcessor delegates to GeminiVideoProcessor."""
        router = AsyncMock()
        router.complete.return_value = MagicMock(content="[0:00-0:05] Test")

        proc = VideoProcessor()
        doc = await proc.process(_make_source(), router=router)

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == "video"

    async def test_video_processor_fallback_no_whisper(self) -> None:
        """Re-raises if whisper is None and Gemini fails."""
        router = AsyncMock()
        router.complete.side_effect = RuntimeError("Gemini down")

        proc = VideoProcessor()
        assert proc._whisper is None

        with pytest.raises(RuntimeError, match="Gemini down"):
            await proc.process(_make_source(), router=router)
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── __init__.py              # exports (S1-011)
├── base.py                  # ABC + exceptions (S1-011)
└── video.py                 # GeminiVideoProcessor, VideoProcessor

tests/unit/test_ingestion/
├── __init__.py
├── test_schemas.py          # S1-011
└── test_video.py            # ~8 tests
```

---

## Кроки виконання

1. Переконатися, що S1-011 завершено (schemas + ABC доступні)
2. Реалізувати `GeminiVideoProcessor` в `ingestion/video.py`
3. Реалізувати `VideoProcessor` shell (Gemini + placeholder whisper)
4. Створити `tests/unit/test_ingestion/test_video.py`
5. `make check`

---

## Примітки

- **Gemini File API**: реальна інтеграція складна (upload → poll status → get URI). У цій задачі `_upload_video` — placeholder, конкретну реалізацію можна дописати при integration testing.
- **TRANSCRIPT_PROMPT**: визначений як модульна константа для зручності тестування та можливого перенесення у `prompts/` YAML.
- **Composition over inheritance**: `VideoProcessor` НЕ наслідує `GeminiVideoProcessor`, а композує. Це дозволяє незалежне тестування та гнучкий fallback.
- **Action `video_analysis`**: вже зареєстрований у `config/models.yaml` з `requires: [vision, long_context]`.
