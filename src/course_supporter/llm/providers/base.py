"""Abstract LLM provider interface."""

import abc
import time
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from course_supporter.llm.schemas import LLMRequest, LLMResponse

logger = structlog.get_logger()


class StructuredOutputError(Exception):
    """Raised when LLM response cannot be parsed into expected schema.

    Attributes:
        provider: Name of the provider that returned invalid output.
        raw_content: The raw LLM response text that failed validation.
        schema_name: Name of the expected Pydantic model.
    """

    def __init__(
        self,
        provider: str,
        raw_content: str,
        schema_name: str,
        cause: ValidationError,
    ) -> None:
        self.provider = provider
        self.raw_content = raw_content
        self.schema_name = schema_name
        super().__init__(f"{provider}: failed to parse response as {schema_name}")
        self.__cause__ = cause


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

    def _parse_structured(
        self,
        raw_json: str,
        response_schema: type[BaseModel],
    ) -> Any:
        """Parse raw JSON into a Pydantic model with error logging.

        Raises:
            StructuredOutputError: If the response is not valid JSON
                or doesn't match the schema. Retry logic is handled
                by ModelRouter (S1-009), not individual providers.
        """
        try:
            return response_schema.model_validate_json(raw_json)
        except ValidationError as exc:
            logger.error(
                "structured_output_parse_failed",
                provider=self.provider_name,
                schema=response_schema.__name__,
                raw_content=raw_json[:500],
            )
            raise StructuredOutputError(
                provider=self.provider_name,
                raw_content=raw_json,
                schema_name=response_schema.__name__,
                cause=exc,
            ) from exc

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
