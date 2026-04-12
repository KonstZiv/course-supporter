"""Deepgram Nova STT provider via HTTP API."""

import asyncio
import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx
import structlog

from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment
from course_supporter.stt.utils import guess_content_type

logger = structlog.get_logger()


class DeepgramSTTProvider(STTProvider):
    """Deepgram Nova STT via binary POST.

    When multiple API keys are provided, httpx clients are
    pre-created and rotated in round-robin order per request.
    """

    provider_name = "deepgram"

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str = "nova-3",
    ) -> None:
        super().__init__()
        self._clients = tuple(
            httpx.AsyncClient(
                base_url="https://api.deepgram.com",
                headers={"Authorization": f"Token {k}"},
                timeout=httpx.Timeout(120.0),
            )
            for k in api_keys
        )
        self._client_cycle: Iterator[httpx.AsyncClient] = itertools.cycle(self._clients)
        self._default_model = default_model

    def _next_client(self) -> httpx.AsyncClient:
        return next(self._client_cycle)

    async def transcribe(self, request: STTRequest) -> STTResult:
        """Transcribe audio via Deepgram API."""
        audio_path = Path(request.audio_path)
        content_type = guess_content_type(audio_path.suffix)

        params: dict[str, str | bool] = {
            "model": self._default_model,
            "punctuate": True,
            "paragraphs": True,
            "smart_format": True,
        }
        if request.language:
            params["language"] = request.language
        else:
            # Auto-detect when caller didn't specify the language.
            # httpx encodes True → "true" for query params (lowercase),
            # matching Deepgram's documented boolean format.
            params["detect_language"] = True

        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
        client = self._next_client()
        with self._measure_latency() as timer:
            resp = await client.post(
                "/v1/listen",
                headers={"Content-Type": content_type},
                params=params,
                content=audio_bytes,
            )

        resp.raise_for_status()
        body = resp.json()

        channel = body["results"]["channels"][0]
        alt = channel["alternatives"][0]
        text: str = alt.get("transcript", "")
        confidence: float | None = alt.get("confidence")

        # Duration from metadata.
        metadata = body.get("metadata", {})
        duration: float | None = metadata.get("duration")

        # Auto-detected language (Deepgram reports this at the channel level).
        detected: str | None = None
        if request.language is None:
            raw_detected = channel.get("detected_language")
            if isinstance(raw_detected, str) and raw_detected:
                detected = raw_detected

        segments = _build_segments(alt)

        return STTResult(
            text=text,
            segments=segments,
            language=request.language,
            detected_language=detected,
            confidence=confidence,
            provider=self.provider_name,
            model_id=self._default_model,
            latency_ms=timer.elapsed_ms,
            audio_duration_sec=duration,
        )


def _build_segments(alt: dict[str, object]) -> list[STTSegment]:
    """Build STTSegment list from Deepgram alternative.

    Deepgram returns ``paragraphs.paragraphs[].sentences[]``
    when paragraphs=true. Falls back to word-level merging.
    """
    paragraphs = alt.get("paragraphs")
    if isinstance(paragraphs, dict):
        para_list = paragraphs.get("paragraphs")
        if isinstance(para_list, list):
            return _segments_from_paragraphs(para_list)

    # Fallback: merge words into segments.
    words = alt.get("words")
    if isinstance(words, list):
        return _segments_from_words(words)

    return []


def _segments_from_paragraphs(para_list: list[object]) -> list[STTSegment]:
    """Convert Deepgram paragraphs to STTSegments."""
    segments: list[STTSegment] = []
    for para in para_list:
        if not isinstance(para, dict):
            continue
        sentences = para.get("sentences")
        if not isinstance(sentences, list):
            continue
        for sent in sentences:
            if not isinstance(sent, dict):
                continue
            text = sent.get("text", "")
            start = sent.get("start", 0.0)
            end = sent.get("end", 0.0)
            if (
                text
                and isinstance(start, (int, float))
                and isinstance(end, (int, float))
            ):
                segments.append(
                    STTSegment(
                        start_sec=round(float(start), 2),
                        end_sec=round(float(end), 2),
                        text=str(text).strip(),
                    )
                )
    return segments


def _segments_from_words(words: list[object]) -> list[STTSegment]:
    """Merge Deepgram words into ~15-second segments."""
    segments: list[STTSegment] = []
    seg_words: list[str] = []
    seg_start: float | None = None
    seg_end: float = 0.0
    target = 15.0

    for w in words:
        if not isinstance(w, dict):
            continue
        text = w.get("word", "")
        start = w.get("start", 0.0)
        end = w.get("end", 0.0)
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue

        if seg_start is None:
            seg_start = float(start)
        seg_words.append(str(text))
        seg_end = float(end)

        if seg_end - seg_start >= target:
            segments.append(
                STTSegment(
                    start_sec=round(seg_start, 2),
                    end_sec=round(seg_end, 2),
                    text=" ".join(seg_words).strip(),
                )
            )
            seg_words = []
            seg_start = None

    if seg_words and seg_start is not None:
        segments.append(
            STTSegment(
                start_sec=round(seg_start, 2),
                end_sec=round(seg_end, 2),
                text=" ".join(seg_words).strip(),
            )
        )

    return segments
