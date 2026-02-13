"""Abstract LLM provider interface."""

import abc
import time
from typing import Any

from pydantic import BaseModel

from course_supporter.llm.schemas import LLMRequest, LLMResponse


class LLMProvider(abc.ABC):
    """Base class for all LLM providers.

    Each provider implements two methods:
    - complete(): free-form text generation
    - complete_structured(): generation with Pydantic schema validation

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
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion."""
        ...

    @abc.abstractmethod
    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output validated against Pydantic schema.

        Returns:
            Tuple of (parsed_object, raw_llm_response).
            parsed_object is an instance of response_schema.
        """
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
