"""Local Whisper transcription — heavy step implementation.

Standalone async function that transcribes a WAV audio file using
OpenAI Whisper. Conforms to ``TranscribeFunc`` protocol from
:mod:`course_supporter.ingestion.heavy_steps`.

No DB, S3, or ORM dependencies — pure audio-in, transcript-out.
Can be swapped for a Lambda-based implementation later.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import (
    TranscribeParams,
    Transcript,
    TranscriptSegment,
)

_MODEL_CACHE: dict[str, Any] = {}


async def local_transcribe(
    audio_path: str,
    params: TranscribeParams,
) -> Transcript:
    """Transcribe a WAV audio file using local Whisper model.

    Runs Whisper in a thread pool to avoid blocking the event loop.

    Args:
        audio_path: Path to the WAV audio file on local disk.
        params: Transcription parameters (model size, language).

    Returns:
        Structured transcript with timestamped segments.

    Raises:
        ProcessingError: If Whisper is not installed or transcription fails.
    """
    logger = structlog.get_logger().bind(
        audio_path=audio_path,
        model_name=params.model_name,
        language=params.language,
    )
    logger.info("whisper_transcription_start")

    try:
        import whisper
    except ImportError:
        raise ProcessingError(
            "whisper is not installed. Install with: uv sync --extra media"
        ) from None

    loop = asyncio.get_running_loop()

    model_name = str(params.model_name)
    if model_name not in _MODEL_CACHE:
        _MODEL_CACHE[model_name] = await loop.run_in_executor(
            None, whisper.load_model, model_name
        )
    model = _MODEL_CACHE[model_name]

    transcribe_kwargs: dict[str, str] = {}
    if params.language is not None:
        transcribe_kwargs["language"] = params.language

    result = await loop.run_in_executor(
        None,
        lambda: model.transcribe(audio_path, **transcribe_kwargs),
    )

    raw_segments: list[dict[str, Any]] = result.get("segments", [])
    detected_language: str | None = result.get("language")

    segments: list[TranscriptSegment] = []
    for seg in raw_segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start_sec=round(float(seg.get("start", 0.0)), 2),
                end_sec=round(float(seg.get("end", 0.0)), 2),
                text=text,
            )
        )

    logger.info(
        "whisper_transcription_done",
        segment_count=len(segments),
        detected_language=detected_language,
    )

    return Transcript(
        segments=segments,
        language=detected_language,
    )
