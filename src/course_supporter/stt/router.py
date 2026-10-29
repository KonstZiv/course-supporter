"""STT router with fallback chain and retry logic.

Mirrors ModelRouter architecture but simplified for STT:
- No context window guards (audio has no token limits)
- Cost calculated per minute of audio, not per token
- Single ``transcribe()`` method (no structured output)

Every attempt — the first try, each retry, each fallback model — is one row of
the call register, priced from the audio duration known BEFORE the call
(BIL-C, decision 6): a retried failure is a request the provider may have
billed, so it is never left out.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from course_supporter.llm.pricing import PricedCall, price_audio_call
from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig
from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True, kw_only=True)
class STTAttempt:
    """One transcription attempt, as the call register records it."""

    action: str
    strategy: str
    provider: str
    model_id: str
    latency_ms: int
    priced: PricedCall
    error_message: str | None


LogCallback = Callable[[STTAttempt], Awaitable[None]]
"""Optional async callback for DB logging: one call per attempt."""


def _utc_now() -> datetime:
    """When an attempt is sent; a seam the tests freeze."""
    return datetime.now(UTC)


class AllSTTProvidersFailedError(Exception):
    """Raised when every provider in the chain has failed."""

    def __init__(
        self,
        action: str,
        strategies_tried: list[str],
        errors: list[tuple[str, str]],
    ) -> None:
        self.action = action
        self.strategies_tried = strategies_tried
        self.errors = errors
        summary = "; ".join(f"{m}: {e}" for m, e in errors[:5])
        super().__init__(
            f"All STT providers failed for '{action}' "
            f"(strategies: {strategies_tried}): {summary}"
        )


class STTRouter:
    """Route STT requests through a fallback chain of providers.

    Two-level fallback:
    1. Within the requested strategy chain (model by model).
    2. If not default, fall back to the default strategy chain.

    Each model attempt is retried up to ``max_attempts`` on
    transient errors (429, 5xx, network). Permanent errors
    (400, 401, 403) skip to the next model immediately.
    """

    def __init__(
        self,
        providers: dict[str, STTProvider],
        registry: ModelRegistryConfig,
        log_callback: LogCallback | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._log_callback = log_callback
        self._max_attempts = max_attempts

    async def transcribe(
        self,
        action: str,
        audio_path: str,
        *,
        language: str | None = None,
        strategy: str = "default",
        duration_sec: float | None = None,
    ) -> STTResult:
        """Transcribe an audio file using the configured fallback chain.

        ``duration_sec`` (when the caller probed it — the video path) drives
        the provider's duration-proportional request timeout (task M1);
        ``None`` leaves the provider on its conservative timeout cap.
        """
        request = STTRequest(
            audio_path=audio_path,
            language=language,
            action=action,
            strategy=strategy,
            duration_sec=duration_sec,
        )
        return await self._execute_with_fallback(action, strategy, request)

    # -- internal: strategy fallback ------------------------------------

    async def _execute_with_fallback(
        self,
        action: str,
        strategy: str,
        request: STTRequest,
    ) -> STTResult:
        """Two-level fallback: requested strategy -> default."""
        errors: list[tuple[str, str]] = []
        strategies_tried: list[str] = []

        result = await self._try_chain(action, strategy, request, errors)
        strategies_tried.append(strategy)
        if result is not None:
            return result

        if strategy != "default":
            logger.info(
                "stt_strategy_chain_exhausted",
                action=action,
                failed_strategy=strategy,
                fallback_strategy="default",
            )
            result = await self._try_chain(action, "default", request, errors)
            strategies_tried.append("default")
            if result is not None:
                result.strategy = f"{strategy}->default"
                return result

        raise AllSTTProvidersFailedError(action, strategies_tried, errors)

    # -- internal: chain iteration --------------------------------------

    async def _try_chain(
        self,
        action: str,
        strategy: str,
        request: STTRequest,
        errors: list[tuple[str, str]],
    ) -> STTResult | None:
        """Walk the model chain, trying each active provider."""
        chain = self._registry.get_chain(action, strategy)

        for model_cfg in chain:
            provider = self._get_active_provider(model_cfg, errors)
            if provider is None:
                continue

            result = await self._try_with_retries(
                provider, request, model_cfg, errors, action, strategy
            )
            if result is not None:
                return result

        return None

    def _get_active_provider(
        self,
        model_cfg: ModelConfig,
        errors: list[tuple[str, str]],
    ) -> STTProvider | None:
        """Get provider if it exists and is enabled."""
        provider = self._providers.get(model_cfg.provider)
        if provider is None:
            errors.append((model_cfg.model_id, "provider not configured"))
            return None
        if not provider.enabled:
            errors.append((model_cfg.model_id, "provider disabled"))
            return None
        return provider

    # -- internal: retry loop -------------------------------------------

    async def _try_with_retries(
        self,
        provider: STTProvider,
        request: STTRequest,
        model_cfg: ModelConfig,
        errors: list[tuple[str, str]],
        action: str,
        strategy: str,
    ) -> STTResult | None:
        """Retry transcription up to max_attempts on transient errors.

        Each attempt is written to the register, a failed retry included.
        """
        for attempt in range(1, self._max_attempts + 1):
            started_at = _utc_now()
            started = time.perf_counter()
            try:
                result = await provider.transcribe(request)
            except Exception as exc:
                await self._log_attempt(
                    model_cfg,
                    request,
                    action=action,
                    strategy=strategy,
                    started_at=started_at,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    failure=exc,
                )
                if not self._is_retryable(exc):
                    logger.warning(
                        "stt_call_permanent_error",
                        provider=model_cfg.provider,
                        model=model_cfg.model_id,
                        error=str(exc),
                    )
                    errors.append((model_cfg.model_id, str(exc)))
                    break

                logger.warning(
                    "stt_call_failed",
                    provider=model_cfg.provider,
                    model=model_cfg.model_id,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error=str(exc),
                )
                if attempt == self._max_attempts:
                    errors.append((model_cfg.model_id, str(exc)))
                continue

            priced = self._price(model_cfg, request, started_at, failure=None)
            self._enrich_result(result, action, strategy, priced)
            await self._log_attempt(
                model_cfg,
                request,
                action=action,
                strategy=strategy,
                started_at=started_at,
                latency_ms=result.latency_ms,
                failure=None,
                priced=priced,
            )
            return result
        return None

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Classify exception as transient (retry) or permanent (skip).

        Uses duck typing to check status_code/code on SDK exceptions
        without importing SDK-specific classes.
        """
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code not in (400, 401, 403, 404)

        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code not in (400, 401, 403, 404)

        return True

    def _price(
        self,
        model_cfg: ModelConfig,
        request: STTRequest,
        started_at: datetime,
        *,
        failure: Exception | None,
    ) -> PricedCall:
        """Price one attempt from the duration the caller knew before it."""
        return price_audio_call(
            model_cfg,
            catalog_version=self._registry.catalog_version,
            started_at=started_at,
            duration_sec=request.duration_sec,
            succeeded=failure is None,
            failure=failure,
        )

    @staticmethod
    def _enrich_result(
        result: STTResult,
        action: str,
        strategy: str,
        priced: PricedCall,
    ) -> None:
        """Set action, strategy and the register's price on the result."""
        result.action = action
        result.strategy = strategy
        result.cost_usd = (
            float(priced.cost_usd) if priced.cost_usd is not None else None
        )

    async def _log_attempt(
        self,
        model_cfg: ModelConfig,
        request: STTRequest,
        *,
        action: str,
        strategy: str,
        started_at: datetime,
        latency_ms: int,
        failure: Exception | None,
        priced: PricedCall | None = None,
    ) -> None:
        """Write one attempt via the callback; a callback failure is only logged."""
        if self._log_callback is None:
            return
        try:
            await self._log_callback(
                STTAttempt(
                    action=action,
                    strategy=strategy,
                    provider=model_cfg.provider,
                    model_id=model_cfg.model_id,
                    latency_ms=latency_ms,
                    priced=priced
                    or self._price(model_cfg, request, started_at, failure=failure),
                    error_message=(
                        None
                        if failure is None
                        else str(failure).strip() or type(failure).__name__
                    ),
                )
            )
        except Exception:
            logger.exception("stt_log_callback_error")

    def available_providers(self) -> list[str]:
        """Return names of configured and enabled providers."""
        return [name for name, p in self._providers.items() if p.enabled]
