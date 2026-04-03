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
from course_supporter.ingestion.heavy_steps import (
    TranscribeFunc,
    TranscribeParams,
    TranscriptSegment,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import MaterialEntry

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
        source: MaterialEntry,
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
                        start_sec=_timecode_to_seconds(start_str),
                        end_sec=_timecode_to_seconds(end_str),
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
    using an injected ``TranscribeFunc`` (default: ``local_transcribe``).

    Requires:
    - FFmpeg installed on the system
    - whisper package (optional 'media' dependency group)
    """

    def __init__(
        self,
        *,
        transcribe_func: TranscribeFunc | None = None,
    ) -> None:
        self._transcribe_func = transcribe_func or self._default_transcribe_func()

    @staticmethod
    def _default_transcribe_func() -> TranscribeFunc:
        """Lazy-import local_transcribe as the default implementation."""
        from course_supporter.ingestion.transcribe import local_transcribe

        return local_transcribe

    async def process(
        self,
        source: MaterialEntry,
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
            # 2. Transcribe audio via injected heavy step
            transcript = await self._transcribe_func(audio_path, TranscribeParams())

            # 3. Convert segments to chunks
            chunks = self._segments_to_chunks(transcript.segments)

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

    @staticmethod
    def _segments_to_chunks(
        segments: list[TranscriptSegment],
    ) -> list[ContentChunk]:
        """Convert TranscriptSegment objects to ContentChunks."""
        chunks: list[ContentChunk] = []
        for idx, seg in enumerate(segments):
            text = seg.text.strip()
            if not text:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text=text,
                    index=idx,
                    start_sec=round(seg.start_sec, 2),
                    end_sec=round(seg.end_sec, 2),
                )
            )
        return chunks


class VideoProcessor(SourceProcessor):
    """Composite video processor: parallel STT + VD.

    Runs audio transcription (STT) and visual description (VD) as
    independent parallel streams. Both produce ContentChunks with
    timestamps that can be aligned downstream (VD-007).

    Graceful degradation: if VD fails, STT-only result is returned.

    Args:
        stt: STT processor (WhisperVideoProcessor or similar).
        vd_pipeline: Optional VDPipeline for visual analysis.
            When None, only STT chunks are produced.
    """

    def __init__(
        self,
        *,
        stt: WhisperVideoProcessor | None = None,
        vd_pipeline: object | None = None,
    ) -> None:
        self._stt = stt or WhisperVideoProcessor()
        self._vd = vd_pipeline

    async def process(
        self,
        source: MaterialEntry,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.VIDEO:
            raise UnsupportedFormatError(
                f"VideoProcessor expects 'video', got '{source.source_type}'"
            )

        logger.info("video_processor_start", source_url=source.source_url)

        # Launch STT and VD in parallel
        stt_task = asyncio.create_task(
            self._stt.process(source, router=router),
        )
        vd_task = self._run_vd(source) if self._vd is not None else None

        # Wait for STT (required)
        stt_doc = await stt_task
        stt_chunks = stt_doc.chunks

        # Wait for VD (optional, graceful degradation)
        vd_chunks: list[ContentChunk] = []
        if vd_task is not None:
            try:
                vd_chunks = await vd_task
            except Exception:
                logger.exception(
                    "vd_pipeline_failed_stt_only",
                    source_url=source.source_url,
                )

        strategy = "stt+vd" if vd_chunks else "stt"
        logger.info(
            "video_processor_done",
            source_url=source.source_url,
            stt_chunks=len(stt_chunks),
            vd_chunks=len(vd_chunks),
            strategy=strategy,
        )

        return SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.filename or "",
            chunks=stt_chunks + vd_chunks,
            metadata={
                "strategy": strategy,
                "stt_segments": len(stt_chunks),
                "vd_scenes": len(vd_chunks),
            },
        )

    async def _run_vd(self, source: MaterialEntry) -> list[ContentChunk]:
        """Run VD pipeline and convert result to ContentChunks."""
        from course_supporter.vd.pipeline import VDPipeline

        vd: VDPipeline = self._vd  # type: ignore[assignment]

        # VDPipeline needs a local file path
        video_path = Path(source.source_url)
        result = await vd.process(video_path)

        return self._vd_result_to_chunks(result)

    @staticmethod
    def _vd_result_to_chunks(result: object) -> list[ContentChunk]:
        """Convert VDResult scenes to VISUAL_SCENE ContentChunks."""
        from course_supporter.vd.schemas import VDResult

        vd_result: VDResult = result  # type: ignore[assignment]
        chunks: list[ContentChunk] = []

        for idx, analysis in enumerate(vd_result.scenes):
            sm = analysis.scene_memory
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.VISUAL_SCENE,
                    text=sm.summary,
                    index=idx,
                    start_sec=analysis.scene.start_sec,
                    end_sec=analysis.scene.end_sec,
                    metadata={
                        "scene_id": analysis.scene.scene_id,
                        "scene_type": sm.scene_type,
                        "importance": sm.importance,
                        "topics": sm.topics,
                        "frames_analyzed": len(analysis.eyes_results),
                    },
                ),
            )

        return chunks


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
