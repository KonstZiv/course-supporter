"""Video processor implementations.

Two strategies:
- GeminiVideoProcessor: Upload video to Gemini File API -> vision transcript
- WhisperVideoProcessor: FFmpeg audio extraction -> Whisper (S1-013)

VideoProcessor composes both with automatic fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

        # 1. Build multimodal contents with video URL + prompt
        from google.genai import types as genai_types

        contents: list[Any] = [
            genai_types.Content(
                parts=[
                    genai_types.Part(
                        file_data=genai_types.FileData(
                            file_uri=source.source_url,
                        ),
                    ),
                    genai_types.Part(text=TRANSCRIPT_PROMPT),
                ],
            ),
        ]

        # 2. Call LLM with video content
        response = await router.complete(
            action="video_analysis",
            prompt=TRANSCRIPT_PROMPT,
            contents=contents,
        )

        # 3. Check if Gemini actually saw the video
        #    When video is too long or unavailable, Gemini returns
        #    a hallucinated response with very few input tokens.
        min_video_tokens = 1000
        if (response.tokens_in or 0) < min_video_tokens:
            logger.warning(
                "gemini_video_not_processed",
                source_url=source.source_url,
                tokens_in=response.tokens_in,
            )
            raise ProcessingError(
                f"Gemini did not process the video "
                f"(tokens_in={response.tokens_in}, "
                f"threshold={min_video_tokens})"
            )

        # 4. Parse transcript into chunks
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
                logger.warning(
                    "transcript_line_without_timecode",
                    line=line,
                )
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.TRANSCRIPT,
                        text=line,
                        index=idx,
                    )
                )
            idx += 1

        return chunks


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
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.VIDEO:
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
                source_type=SourceType.VIDEO,
                source_url=source.source_url,
                title=source.filename or "",
                chunks=chunks,
                metadata={"strategy": "whisper"},
            )
        finally:
            # Clean up temp audio file
            with contextlib.suppress(OSError):
                Path(audio_path).unlink()  # noqa: ASYNC240

    async def _extract_audio(self, video_source: str) -> str:
        """Extract audio from a video file or URL.

        For URLs (http/https), downloads audio via yt-dlp first.
        For local files, extracts audio directly via FFmpeg.

        Args:
            video_source: Path or URL to the video.

        Returns:
            Path to the extracted WAV audio file.

        Raises:
            ProcessingError: If yt-dlp/FFmpeg is not found or fails.
        """
        if video_source.startswith(("http://", "https://")):
            video_path = await self._download_audio(video_source)
        else:
            video_path = video_source

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            audio_path = tmp.name

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                video_path,
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                audio_path,
                "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                raise ProcessingError(
                    f"FFmpeg failed (code {process.returncode}): "
                    f"{stderr.decode()[:500]}"
                )
        except FileNotFoundError:
            raise ProcessingError(
                "FFmpeg not found. Install FFmpeg to use WhisperVideoProcessor."
            ) from None
        finally:
            # Clean up yt-dlp downloaded file
            if video_source.startswith(("http://", "https://")):
                with contextlib.suppress(OSError):
                    Path(video_path).unlink()  # noqa: ASYNC240

        return audio_path

    async def _download_audio(self, url: str) -> str:
        """Download audio from a video URL using yt-dlp.

        Downloads audio-only stream to a temporary file.
        Supports YouTube, Vimeo, and hundreds of other sites.

        Args:
            url: Video URL.

        Returns:
            Path to the downloaded audio file.

        Raises:
            ProcessingError: If yt-dlp is not found or download fails.
        """
        with tempfile.NamedTemporaryFile(suffix=".%(ext)s", delete=False) as tmp:
            output_template = tmp.name.replace(".%(ext)s", ".%(ext)s")

        # yt-dlp: extract audio only, best quality, no video
        # Use `python -m yt_dlp` instead of `yt-dlp` binary to avoid
        # shebang issues in Docker multi-stage builds.
        import sys

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "yt_dlp",
                "--extract-audio",
                "--audio-format",
                "wav",
                "--output",
                output_template,
                "--no-playlist",
                "--quiet",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                raise ProcessingError(
                    f"yt-dlp failed (code {process.returncode}): "
                    f"{stderr.decode()[:500]}"
                )
        except FileNotFoundError:
            raise ProcessingError(
                "yt-dlp not found. Install yt-dlp to process video URLs."
            ) from None

        # yt-dlp replaces %(ext)s with actual extension
        output_path = output_template.replace("%(ext)s", "wav")
        if not Path(output_path).exists():  # noqa: ASYNC240
            raise ProcessingError(f"yt-dlp output file not found: {output_path}")

        logger.info("ytdlp_audio_downloaded", url=url, path=output_path)
        return output_path

    async def _transcribe(self, audio_path: str) -> list[dict[str, Any]]:
        """Transcribe audio file using Whisper.

        Runs Whisper in a thread pool to avoid blocking the event loop.

        Args:
            audio_path: Path to the WAV audio file.

        Returns:
            List of segment dicts with 'start', 'end', 'text' keys.
        """
        import whisper

        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(None, whisper.load_model, "base")
        result = await loop.run_in_executor(None, model.transcribe, audio_path)
        return result.get("segments", [])  # type: ignore[no-any-return]

    @staticmethod
    def _segments_to_chunks(
        segments: list[dict[str, Any]],
    ) -> list[ContentChunk]:
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


class VideoProcessor(SourceProcessor):
    """Composite video processor with Gemini primary + Whisper fallback.

    Tries GeminiVideoProcessor first. If it fails and Whisper is enabled,
    falls back to WhisperVideoProcessor (local FFmpeg + Whisper).
    """

    def __init__(self, *, enable_whisper: bool = True) -> None:
        self._gemini = GeminiVideoProcessor()
        self._whisper: SourceProcessor | None = (
            WhisperVideoProcessor() if enable_whisper else None
        )

    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        try:
            return await self._gemini.process(source, router=router)
        except UnsupportedFormatError:
            raise
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
