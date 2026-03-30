"""Abstract STT provider interface."""

import abc
import time

from course_supporter.stt.schemas import STTRequest, STTResult


class STTProvider(abc.ABC):
    """Base class for all STT providers.

    Each provider implements a single method:
    - transcribe(): audio file → transcript text with segments

    Providers support runtime enable/disable for handling
    rate limits, quota exhaustion, or API outages.
    """

    provider_name: str = ""

    def __init__(self) -> None:
        self._enabled: bool = True

    @property
    def enabled(self) -> bool:
        """Whether this provider is currently available."""
        return self._enabled

    def disable(self, reason: str = "") -> None:
        """Disable provider at runtime (rate limit, API down, etc.)."""
        self._enabled = False

    def enable(self) -> None:
        """Re-enable provider."""
        self._enabled = True

    @abc.abstractmethod
    async def transcribe(self, request: STTRequest) -> STTResult:
        """Transcribe an audio file to text with timestamps."""
        ...

    def _measure_latency(self) -> "_LatencyTimer":
        """Context manager for measuring call latency."""
        return _LatencyTimer()


class _LatencyTimer:
    """Simple latency measurement helper."""

    def __init__(self) -> None:
        self.start: float = 0
        self.elapsed_ms: int = 0

    def __enter__(self) -> "_LatencyTimer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
