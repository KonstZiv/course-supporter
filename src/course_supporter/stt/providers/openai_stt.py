"""OpenAI STT provider (GPT-4o Mini Transcribe, Whisper)."""

import asyncio
import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path

import openai
import structlog

from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment

logger = structlog.get_logger()

_MIME_BY_EXT: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
}


class OpenAISTTProvider(STTProvider):
    """OpenAI transcription via official SDK.

    Supports both ``gpt-4o-mini-transcribe`` and ``whisper-1`` models.
    When multiple API keys are provided, clients are rotated
    in round-robin order.
    """

    provider_name = "openai_stt"

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str = "gpt-4o-mini-transcribe",
    ) -> None:
        super().__init__()
        self._clients = tuple(openai.AsyncOpenAI(api_key=k) for k in api_keys)
        self._client_cycle: Iterator[openai.AsyncOpenAI] = itertools.cycle(
            self._clients
        )
        self._default_model = default_model

    def _next_client(self) -> openai.AsyncOpenAI:
        return next(self._client_cycle)

    async def transcribe(self, request: STTRequest) -> STTResult:
        """Transcribe audio via OpenAI Transcription API."""
        audio_path = Path(request.audio_path)
        model = self._default_model

        # gpt-4o-mini-transcribe does not support verbose_json.
        response_format = "verbose_json" if model.startswith("whisper") else "json"

        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
        suffix = audio_path.suffix or ".wav"
        mime = _MIME_BY_EXT.get(suffix, "audio/wav")

        client = self._next_client()
        with self._measure_latency() as timer:
            result = await client.audio.transcriptions.create(  # type: ignore[call-overload]
                model=model,
                file=(f"audio{suffix}", audio_bytes, mime),
                language=request.language or "",
                response_format=response_format,
            )

        text: str = result.text if hasattr(result, "text") else str(result)
        segments = _extract_segments(result)

        # whisper-1 verbose_json returns `language`; gpt-4o-mini-transcribe does not.
        detected_raw = getattr(result, "language", None)
        detected: str | None = None
        if request.language is None and isinstance(detected_raw, str) and detected_raw:
            detected = detected_raw

        # KD-2.2-C §1.N2 collateral fix: populate audio_duration_sec for
        # whisper-* verbose_json responses (top-level ``duration`` field).
        # gpt-4o-mini-transcribe (json response_format) does not surface
        # duration; remains None.
        raw_duration = getattr(result, "duration", None)
        audio_duration_sec = (
            float(raw_duration) if isinstance(raw_duration, (int, float)) else None
        )

        return STTResult(
            text=text,
            segments=segments,
            language=request.language,
            detected_language=detected,
            audio_duration_sec=audio_duration_sec,
            provider=self.provider_name,
            model_id=model,
            latency_ms=timer.elapsed_ms,
        )


def _extract_segments(result: object) -> list[STTSegment]:
    """Extract segments from OpenAI transcription result.

    verbose_json (Whisper) returns segments with timestamps.
    json (GPT-4o) returns only text — no segments available.
    """
    raw_segments = getattr(result, "segments", None)
    if not raw_segments or not isinstance(raw_segments, list):
        return []

    segments: list[STTSegment] = []
    for seg in raw_segments:
        start = getattr(seg, "start", None)
        end = getattr(seg, "end", None)
        text = getattr(seg, "text", "")
        if start is not None and end is not None and text:
            segments.append(
                STTSegment(
                    start_sec=round(float(start), 2),
                    end_sec=round(float(end), 2),
                    text=str(text).strip(),
                )
            )
    return segments
