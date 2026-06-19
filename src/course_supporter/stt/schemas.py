"""STT request/response schemas."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class STTRequest(BaseModel):
    """Input for an STT transcription call.

    Attributes:
        audio_path: Path to audio file on local disk (MP3, WAV, etc.).
        language: ISO 639-1 language code. None = auto-detect.
        action: Routing key for strategy selection (mirrors LLMRequest).
        strategy: Strategy name for fallback chain selection.
        duration_sec: Source audio duration in seconds, when known to the
            caller (the video path probes it via ffprobe). Drives the
            duration-proportional request timeout (task M1). ``None`` when
            the caller cannot probe it pre-STT (pure-audio path) → the
            provider falls back to the conservative timeout cap.
    """

    audio_path: str
    language: str | None = Field(
        default=None,
        description="ISO 639-1 language code. None = auto-detect.",
        pattern=r"^[a-z]{2}$",
    )
    action: str = "transcribe"
    strategy: str = "default"
    duration_sec: float | None = Field(
        default=None,
        description="Source audio duration (s), when probed by the caller.",
    )


class STTSegment(BaseModel):
    """Single timestamped segment of a transcript."""

    start_sec: float
    end_sec: float
    text: str


class STTWord(BaseModel):
    """Single STT word output — KD-2.2-C primitive of ``STTResult.words``.

    STT-domain primitive emitted by the provider with per-word time
    anchors (start_sec / end_sec). Lives at the top of
    :class:`STTResult` (not per-segment). Position index is implicit
    via ``enumerate(STTResult.words)``; no explicit ``word_idx``
    field. ``logprob`` is provider-optional — ElevenLabs Scribe v2
    surfaces word-level log probability, other providers may omit
    it. Consumed by the Pass 2a audio pipeline through the
    ``word_idx`` bridge (KD-2.2-F, helper lives in
    ``ingestion/audio.py``).
    """

    start_sec: float = Field(description="Word start time (seconds).")
    end_sec: float = Field(
        description="Word end time (seconds, > start_sec).",
    )
    text: str = Field(description="Word text as transcribed by STT.")
    logprob: float | None = Field(
        default=None,
        description="Word-level log probability if surfaced by provider.",
    )


class STTResult(BaseModel):
    """Output of an STT transcription call.

    Mirrors LLMResponse structure for logging compatibility.
    """

    text: str
    segments: list[STTSegment] = Field(default_factory=list)
    words: list[STTWord] = Field(
        default_factory=list,
        description=(
            "Per-word transcription with provider time anchors "
            "(KD-2.2-C). Top-level optional field surfaced when "
            "the provider exposes word-level alignment (ElevenLabs "
            "Scribe, Deepgram). Empty list when not surfaced."
        ),
    )
    language: str | None = None
    detected_language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code reported by the provider when auto-detection "
            "was used (language=None in request). Upstream code can cache "
            "this on the material for future STT calls."
        ),
    )
    confidence: float | None = None
    provider: str
    model_id: str
    latency_ms: int = 0
    audio_duration_sec: float | None = None
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
