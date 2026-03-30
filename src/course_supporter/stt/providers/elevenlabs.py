"""ElevenLabs Scribe STT provider via HTTP API."""

import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx
import structlog

from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment

logger = structlog.get_logger()

# ElevenLabs uses ISO 639-3 codes; map from ISO 639-1.
_LANG_MAP: dict[str, str] = {
    "uk": "ukr",
    "en": "eng",
    "ru": "rus",
    "pl": "pol",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
}


class ElevenLabsSTTProvider(STTProvider):
    """ElevenLabs Scribe STT via multipart file upload.

    When multiple API keys are provided, httpx clients are
    pre-created and rotated in round-robin order per request.
    """

    provider_name = "elevenlabs"

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str = "scribe_v1",
    ) -> None:
        super().__init__()
        self._clients = tuple(
            httpx.AsyncClient(
                base_url="https://api.elevenlabs.io",
                headers={"xi-api-key": k},
                timeout=httpx.Timeout(120.0),
            )
            for k in api_keys
        )
        self._client_cycle: Iterator[httpx.AsyncClient] = itertools.cycle(self._clients)
        self._default_model = default_model

    def _next_client(self) -> httpx.AsyncClient:
        return next(self._client_cycle)

    async def transcribe(self, request: STTRequest) -> STTResult:
        """Transcribe audio via ElevenLabs Scribe API."""
        audio_path = Path(request.audio_path)
        content_type = _guess_content_type(audio_path.suffix)

        lang_code = (
            _LANG_MAP.get(request.language, request.language)
            if request.language
            else None
        )

        data: dict[str, str] = {"model_id": self._default_model}
        if lang_code:
            data["language_code"] = lang_code

        client = self._next_client()
        with self._measure_latency() as timer, audio_path.open("rb") as f:  # noqa: ASYNC230
            resp = await client.post(
                "/v1/speech-to-text",
                files={"file": (audio_path.name, f, content_type)},
                data=data,
            )

        resp.raise_for_status()
        body = resp.json()

        text: str = body.get("text", "")
        detected_lang = body.get("language_code")

        # Convert ISO 639-3 back to 639-1 if possible.
        lang_out = _reverse_lang(detected_lang) if detected_lang else None

        segments = _build_segments(body)

        return STTResult(
            text=text,
            segments=segments,
            language=lang_out,
            provider=self.provider_name,
            model_id=self._default_model,
            latency_ms=timer.elapsed_ms,
        )


def _guess_content_type(suffix: str) -> str:
    """Map file extension to MIME type."""
    mapping: dict[str, str] = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")


def _reverse_lang(iso3: str) -> str | None:
    """Convert ISO 639-3 back to 639-1 (best-effort)."""
    reverse = {v: k for k, v in _LANG_MAP.items()}
    return reverse.get(iso3)


def _build_segments(body: dict[str, object]) -> list[STTSegment]:
    """Build STTSegment list from ElevenLabs response.

    ElevenLabs returns a ``words`` array with per-word timestamps.
    We merge words into ~15-second segments for consistency.
    """
    words = body.get("words")
    if not words or not isinstance(words, list):
        return []

    segments: list[STTSegment] = []
    segment_words: list[str] = []
    segment_start: float | None = None
    segment_end: float = 0.0

    target_duration = 15.0  # seconds

    for w in words:
        if not isinstance(w, dict):
            continue
        text = w.get("text", "")
        start = w.get("start", 0.0)
        end = w.get("end", 0.0)

        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue

        if segment_start is None:
            segment_start = float(start)

        segment_words.append(str(text))
        segment_end = float(end)

        if segment_end - segment_start >= target_duration:
            segments.append(
                STTSegment(
                    start_sec=round(segment_start, 2),
                    end_sec=round(segment_end, 2),
                    text=" ".join(segment_words).strip(),
                )
            )
            segment_words = []
            segment_start = None

    # Flush remaining words.
    if segment_words and segment_start is not None:
        segments.append(
            STTSegment(
                start_sec=round(segment_start, 2),
                end_sec=round(segment_end, 2),
                text=" ".join(segment_words).strip(),
            )
        )

    return segments
