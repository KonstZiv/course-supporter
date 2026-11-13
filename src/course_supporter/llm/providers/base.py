"""Abstract LLM provider interface."""

import abc
import time
from typing import Any

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class RequestConfigError(ValueError):
    """A request its connector cannot send as configured (task 09a).

    Raised by :meth:`LLMProvider.check_request` BEFORE any call, so nothing is
    paid for. The router lets it propagate instead of descending the ladder:
    the next rung would be built from the same stage configuration.
    """


class LLMProvider(abc.ABC):
    """Base class for all LLM providers.

    Each provider implements one call method, :meth:`complete`. Structured
    output goes through it too: the stage sets ``response_schema`` on the
    request, the router picks the schema mode, and the stage's
    ``response_validator`` checks the reply (task 09a).

    Providers support runtime enable/disable for handling
    rate limits, quota exhaustion, or API outages.
    """

    provider_name: str = ""

    # Override in subclasses where the API requires max_tokens
    # (e.g. Anthropic). Used by the StageRouter token guard when neither
    # request nor model config specifies a value.
    default_max_output_tokens: int | None = None

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

    def seed_supported(self, request: LLMRequest) -> bool:
        """Whether this connector can put a reproducibility seed on ``request``.

        Asked by the router on the requests of a ``deterministic`` stage
        (:mod:`course_supporter.llm.reproducibility`); the seed is set only
        where this says ``True``. Default: no — a vendor counts as taking a
        seed only when its own documentation says so, and the connector that
        overrides this cites the page and the date it was read. The request is
        passed because support can depend on it (DashScope documents the seed
        for its text endpoint, not its multimodal one).
        """
        return False

    def check_request(self, request: LLMRequest) -> None:
        """Refuse a request this connector cannot send as configured.

        Called by the router after building the request and before the call.
        Default: a request is sendable unless it carries a seed this connector
        cannot send (:meth:`seed_supported`) — a seed silently dropped would
        read in the register as a reproducible call that was not one. A
        connector whose vendor rejects another combination at the API (and
        would bill or fail late for it) overrides, calls this one first, and
        raises :class:`RequestConfigError`.
        """
        if request.seed is not None and not self.seed_supported(request):
            msg = (
                f"{self.provider_name}: this connector sends no reproducibility "
                f"seed (stage '{request.action}'); the router sets one only "
                "where seed_supported() allows it"
            )
            raise RequestConfigError(msg)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Classify provider exception into a ladder fallback category.

        Default returns ``ErrorCategory.SEMANTIC`` -- immediate
        fallback, no retry. Subclasses should override to identify
        their SDK's specific infrastructure / structural /
        input-overflow exceptions.

        Returning SEMANTIC for unknown exceptions is the safe choice:
        it avoids retry loops on errors the classifier hasn't seen.
        """
        return ErrorCategory.SEMANTIC

    @classmethod
    def supports_reasoning(cls, reasoning: dict[str, Any]) -> bool:
        """Whether this connector can translate a ladder ``reasoning`` form.

        The startup ladder check (P6,
        :func:`course_supporter.llm.ladder_config.validate_ladders_against_registry`)
        calls this per rung that carries a non-``None`` ``reasoning`` form,
        provider-agnostically, to refuse booting a config the connector
        cannot honour. Default: no provider translates any form — only the
        connector that owns a vendor dialect (DashScope) overrides. The
        knowledge of which forms are covered lives in the provider module,
        never duplicated in the validator.
        """
        return False

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
