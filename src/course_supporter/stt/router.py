"""STT router with fallback chain and retry logic.

Mirrors ModelRouter architecture but simplified for STT:
- No context window guards (audio has no token limits)
- Cost calculated per minute of audio, not per token
- Single ``transcribe()`` method (no structured output)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig
from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.schemas import STTRequest, STTResult

logger = structlog.get_logger()

LogCallback = Callable[[STTResult | None, str | None], Awaitable[None]]
"""Optional async callback for DB logging: (result_or_none, error_or_none)."""


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
        """Retry transcription up to max_attempts on transient errors."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await provider.transcribe(request)
                self._enrich_result(result, model_cfg, action, strategy)
                await self._log_success(result)
                return result
            except Exception as exc:
                if not self._is_retryable(exc):
                    logger.warning(
                        "stt_call_permanent_error",
                        provider=model_cfg.provider,
                        model=model_cfg.model_id,
                        error=str(exc),
                    )
                    errors.append((model_cfg.model_id, str(exc)))
                    await self._log_failure(model_cfg, request, str(exc))
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
                    await self._log_failure(model_cfg, request, str(exc))
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

    @staticmethod
    def _enrich_result(
        result: STTResult,
        model_cfg: ModelConfig,
        action: str,
        strategy: str,
    ) -> None:
        """Set action, strategy, and estimate cost on result."""
        result.action = action
        result.strategy = strategy

        if result.audio_duration_sec is not None:
            cost_per_min = getattr(model_cfg, "cost_per_minute", None)
            if isinstance(cost_per_min, (int, float)):
                result.cost_usd = round(
                    result.audio_duration_sec / 60 * float(cost_per_min), 6
                )

    async def _log_success(self, result: STTResult) -> None:
        """Log successful transcription via callback."""
        if self._log_callback is not None:
            try:
                await self._log_callback(result, None)
            except Exception:
                logger.exception("stt_log_callback_error")

    async def _log_failure(
        self,
        model_cfg: ModelConfig,
        request: STTRequest,
        error_msg: str,
    ) -> None:
        """Log failed transcription via callback."""
        if self._log_callback is not None:
            try:
                partial = STTResult(
                    text="",
                    provider=model_cfg.provider,
                    model_id=model_cfg.model_id,
                    action=request.action,
                    strategy=request.strategy,
                )
                await self._log_callback(partial, error_msg)
            except Exception:
                logger.exception("stt_log_callback_error")

    def available_providers(self) -> list[str]:
        """Return names of configured and enabled providers."""
        return [name for name, p in self._providers.items() if p.enabled]
