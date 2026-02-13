"""Video processor implementations.

Two strategies:
- GeminiVideoProcessor: Upload video to Gemini File API -> vision transcript
- WhisperVideoProcessor: FFmpeg audio extraction -> Whisper (S1-013)

VideoProcessor composes both with automatic fallback.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

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
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import SourceMaterial

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

# Pattern: [MM:SS-MM:SS] or [H:MM:SS-H:MM:SS]
_TIMECODE_PATTERN = re.compile(
    r"\[(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)"
)


class GeminiVideoProcessor(SourceProcessor):
    """Process video using Gemini Vision API.

    Uploads video to Gemini File API, sends to vision model
    for transcription with timestamps, parses response into
    ContentChunks.
    """

    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.VIDEO:
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
            source_type=SourceType.VIDEO,
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

        .. note::
            Placeholder implementation. Real Gemini File API upload
            (google.genai.Client().files.upload) will be added during
            integration testing.
        """
        return source_url

    @staticmethod
    def _parse_transcript(raw_text: str) -> list[ContentChunk]:
        """Parse timestamped transcript into ContentChunks.

        Expected format per line: [MM:SS-MM:SS] text content

        Args:
            raw_text: Raw transcript text from Gemini.

        Returns:
            List of ContentChunks with TRANSCRIPT type and timecode metadata.
        """
        chunks: list[ContentChunk] = []
        idx = 0

        for line in raw_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            match = _TIMECODE_PATTERN.match(line)
            if match:
                start_str, end_str, text = match.groups()
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.TRANSCRIPT,
                        text=text.strip(),
                        index=idx,
                        metadata={
                            "start_sec": _timecode_to_seconds(start_str),
                            "end_sec": _timecode_to_seconds(end_str),
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
            idx += 1

        return chunks


class VideoProcessor(SourceProcessor):
    """Composite video processor with Gemini primary + Whisper fallback.

    Tries GeminiVideoProcessor first. If it fails and Whisper is enabled,
    falls back to WhisperVideoProcessor (local FFmpeg + Whisper).
    """

    def __init__(self, *, enable_whisper: bool = False) -> None:
        self._gemini = GeminiVideoProcessor()
        # WhisperVideoProcessor will be connected in S1-013
        self._whisper: SourceProcessor | None = None

    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
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


def _timecode_to_seconds(timecode: str) -> float:
    """Convert timecode string to seconds.

    Supports MM:SS and H:MM:SS formats.

    Args:
        timecode: Time string like "1:30" or "1:02:30".

    Returns:
        Total seconds as float.
    """
    parts = timecode.split(":")
    if len(parts) == 2:
        return float(int(parts[0]) * 60 + int(parts[1]))
    if len(parts) == 3:
        return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    return 0.0
