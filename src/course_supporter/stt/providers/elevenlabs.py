"""ElevenLabs Scribe STT provider via HTTP API."""

import asyncio
import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx
import structlog

from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment, STTWord
from course_supporter.stt.utils import guess_content_type, iso639_1_to_3, iso639_3_to_1

logger = structlog.get_logger()


class ElevenLabsSTTProvider(STTProvider):
    """ElevenLabs Scribe STT via multipart file upload.

    When multiple API keys are provided, httpx clients are
    pre-created and rotated in round-robin order per request.
    """

    provider_name = "elevenlabs"

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str = "scribe_v2",
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
        content_type = guess_content_type(audio_path.suffix)

        # ElevenLabs expects ISO 639-3 codes (e.g. "ukr", not "uk").
        lang_code = iso639_1_to_3(request.language) if request.language else None

        data: dict[str, str] = {"model_id": self._default_model}
        if lang_code:
            data["language_code"] = lang_code

        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)

        client = self._next_client()
        with self._measure_latency() as timer:
            resp = await client.post(
                "/v1/speech-to-text",
                files={"file": (audio_path.name, audio_bytes, content_type)},
                data=data,
            )

        resp.raise_for_status()
        body = resp.json()

        text: str = body.get("text", "")
        detected_lang = body.get("language_code")

        # Convert detected ISO 639-3 back to 639-1.
        lang_out = iso639_3_to_1(detected_lang) if detected_lang else None
        # Only surface as detected_language when caller did not set it.
        detected_out = lang_out if request.language is None else None

        words = _build_words(body)
        segments = _build_segments(body)
        # KD-2.2-C: audio_duration_sec derived deterministically from the
        # last word's end-time anchor. Scribe response shape may surface a
        # top-level ``duration`` key, but Scribe v2 contract is unverified
        # in this commit (sub-area #9 smoke gate will confirm). Word-end
        # derivation is empirically reliable whenever words are surfaced.
        audio_duration_sec = words[-1].end_sec if words else None

        return STTResult(
            text=text,
            segments=segments,
            words=words,
            language=lang_out,
            detected_language=detected_out,
            audio_duration_sec=audio_duration_sec,
            provider=self.provider_name,
            model_id=self._default_model,
            latency_ms=timer.elapsed_ms,
        )


def _build_words(body: dict[str, object]) -> list[STTWord]:
    """Build STTWord list from ElevenLabs response top-level ``words``.

    Per KD-2.2-C, ``STTWord`` is the STT-domain primitive surfaced
    top-level on :class:`STTResult` — not merely consumed for the
    ~15-second segment merge in :func:`_build_segments`. ``logprob``
    is parsed defensively: present when the provider surfaces it
    (Scribe variants), ``None`` otherwise.
    """
    raw = body.get("words")
    if not raw or not isinstance(raw, list):
        return []

    words: list[STTWord] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        text = w.get("text", "")
        start = w.get("start", 0.0)
        end = w.get("end", 0.0)
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        raw_logprob = w.get("logprob")
        logprob = float(raw_logprob) if isinstance(raw_logprob, (int, float)) else None
        words.append(
            STTWord(
                start_sec=float(start),
                end_sec=float(end),
                text=str(text),
                logprob=logprob,
            )
        )
    return words


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
