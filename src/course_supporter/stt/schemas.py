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
    """

    audio_path: str
    language: str | None = Field(
        default=None,
        description="ISO 639-1 language code. None = auto-detect.",
        pattern=r"^[a-z]{2}$",
    )
    action: str = "transcribe"
    strategy: str = "default"


class STTSegment(BaseModel):
    """Single timestamped segment of a transcript."""

    start_sec: float
    end_sec: float
    text: str


class STTResult(BaseModel):
    """Output of an STT transcription call.

    Mirrors LLMResponse structure for logging compatibility.
    """

    text: str
    segments: list[STTSegment] = Field(default_factory=list)
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
