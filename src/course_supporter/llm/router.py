"""ModelRouter -- central entry point for all LLM calls.

Two-level fallback:
1. Within chain: model 1 -> model 2 -> model 3
2. Between strategies: quality chain failed -> fallback to default chain
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig
from course_supporter.llm.schemas import LLMRequest, LLMResponse

logger = structlog.get_logger()

LogCallback = Callable[[LLMResponse, bool, str | None], Awaitable[None]]

# Return type for helper methods that inspect result via isinstance.
_RouterResult = LLMResponse | tuple[Any, LLMResponse]

# TypeVar for call_fn-dependent chain methods — lets mypy infer
# concrete return type (LLMResponse or tuple) from the call_fn signature.
_T = TypeVar("_T", LLMResponse, tuple[Any, LLMResponse])


class AllModelsFailedError(Exception):
    """All models in all attempted strategies failed."""

    def __init__(
        self,
        action: str,
        strategies_tried: list[str],
        errors: list[tuple[str, str]],
    ) -> None:
        self.action = action
        self.strategies_tried = strategies_tried
        self.errors = errors
        details = "; ".join(f"{m}: {e}" for m, e in errors)
        super().__init__(
            f"All models failed for action '{action}' "
            f"(strategies: {strategies_tried}): {details}"
        )


class ModelRouter:
    """Routes LLM requests with strategy-based fallback.

    Fallback order:
    1. Try each model in the requested strategy's chain
    2. If all fail AND strategy != "default" -> try default chain
    3. If all fail -> AllModelsFailedError
    """

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        registry: ModelRegistryConfig,
        log_callback: LogCallback | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._log_callback = log_callback
        self._max_attempts = max_attempts

    async def complete(
        self,
        action: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> LLMResponse:
        """Generate text completion with strategy-based fallback."""
        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            action=action,
            strategy=strategy,
        )

        async def call_fn(provider: LLMProvider, req: LLMRequest) -> LLMResponse:
            return await provider.complete(req)

        return await self._execute_with_fallback(
            action,
            strategy,
            request,
            call_fn,
        )

    async def complete_structured(
        self,
        action: str,
        prompt: str,
        response_schema: type[BaseModel],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output with strategy-based fallback."""
        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            action=action,
            strategy=strategy,
        )

        async def call_fn(
            provider: LLMProvider, req: LLMRequest
        ) -> tuple[Any, LLMResponse]:
            return await provider.complete_structured(req, response_schema)

        return await self._execute_with_fallback(
            action,
            strategy,
            request,
            call_fn,
        )

    # -- internal: strategy fallback ------------------------------------

    async def _execute_with_fallback(
        self,
        action: str,
        strategy: str,
        request: LLMRequest,
        call_fn: Callable[[LLMProvider, LLMRequest], Awaitable[_T]],
    ) -> _T:
        """Two-level fallback: requested strategy -> default."""
        errors: list[tuple[str, str]] = []
        strategies_tried: list[str] = []

        # 1. Try requested strategy
        result = await self._try_chain(
            action,
            strategy,
            request,
            call_fn,
            errors,
        )
        strategies_tried.append(strategy)
        if result is not None:
            return result

        # 2. Fallback to default (if not already default)
        if strategy != "default":
            logger.info(
                "strategy_chain_exhausted_falling_back",
                action=action,
                failed_strategy=strategy,
                fallback_strategy="default",
            )
            result = await self._try_chain(
                action,
                "default",
                request,
                call_fn,
                errors,
            )
            strategies_tried.append("default")
            if result is not None:
                self._set_strategy_path(result, f"{strategy}->default")
                return result

        raise AllModelsFailedError(action, strategies_tried, errors)

    # -- internal: chain iteration --------------------------------------

    async def _try_chain(
        self,
        action: str,
        strategy: str,
        request: LLMRequest,
        call_fn: Callable[[LLMProvider, LLMRequest], Awaitable[_T]],
        errors: list[tuple[str, str]],
    ) -> _T | None:
        """Walk the model chain, calling call_fn for each active provider."""
        chain = self._registry.get_chain(action, strategy)
        for model_cfg in chain:
            provider = self._get_active_provider(model_cfg, errors)
            if provider is None:
                continue

            request_for_model = request.model_copy(
                update={"model": model_cfg.model_id},
            )
            result = await self._try_with_retries(
                provider,
                request_for_model,
                model_cfg,
                call_fn,
                errors,
                action,
                strategy,
            )
            if result is not None:
                return result
        return None

    def _get_active_provider(
        self,
        model_cfg: ModelConfig,
        errors: list[tuple[str, str]],
    ) -> LLMProvider | None:
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
        provider: LLMProvider,
        request: LLMRequest,
        model_cfg: ModelConfig,
        call_fn: Callable[[LLMProvider, LLMRequest], Awaitable[_T]],
        errors: list[tuple[str, str]],
        action: str,
        strategy: str,
    ) -> _T | None:
        """Retry call_fn up to max_attempts on transient errors."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await call_fn(provider, request)
                self._enrich_response(result, model_cfg, action, strategy)
                await self._log_success(result)
                return result
            except Exception as exc:
                if not self._is_retryable(exc):
                    logger.warning(
                        "llm_call_permanent_error",
                        provider=model_cfg.provider,
                        model=model_cfg.model_id,
                        error=str(exc),
                    )
                    errors.append((model_cfg.model_id, str(exc)))
                    await self._log_failure(model_cfg, request, str(exc))
                    break

                logger.warning(
                    "llm_call_failed",
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

        Permanent errors (skip model immediately, no retries):
            HTTP 400 (bad request), 401 (auth), 403 (forbidden), 404

        Transient errors (retry up to max_attempts):
            HTTP 429 (rate limit), 500+, network errors

        Uses duck typing (getattr) to avoid importing SDK-specific
        exception classes — works with anthropic, openai, google-genai.
        """
        if isinstance(exc, StructuredOutputError):
            return True

        # anthropic.APIStatusError, openai.APIStatusError
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code not in (400, 401, 403, 404)

        # google-genai exceptions (.code attribute)
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code not in (400, 401, 403, 404)

        # Unknown exception type — default to retryable (fail-safe)
        return True

    @staticmethod
    def _enrich_response(
        result: _RouterResult,
        model_cfg: ModelConfig,
        action: str,
        strategy: str,
    ) -> None:
        """Mutate LLMResponse inside result with cost, action, strategy."""
        response: LLMResponse
        if isinstance(result, LLMResponse):
            response = result
        elif isinstance(result, tuple):
            response = result[1]
        else:
            return

        response.action = action
        response.strategy = strategy
        if response.tokens_in is not None and response.tokens_out is not None:
            response.cost_usd = model_cfg.estimate_cost(
                response.tokens_in,
                response.tokens_out,
            )

    @staticmethod
    def _set_strategy_path(result: _RouterResult, strategy_path: str) -> None:
        """Set strategy on the LLMResponse inside result."""
        if isinstance(result, LLMResponse):
            result.strategy = strategy_path
        elif isinstance(result, tuple) and len(result) == 2:
            result[1].strategy = strategy_path

    async def _log_success(self, result: _RouterResult) -> None:
        """Log successful LLM call."""
        response: LLMResponse
        if isinstance(result, LLMResponse):
            response = result
        elif isinstance(result, tuple):
            response = result[1]
        else:
            return
        await self._log(response, success=True)

    async def _log_failure(
        self,
        model_cfg: ModelConfig,
        request: LLMRequest,
        error_message: str,
    ) -> None:
        """Log failed LLM call."""
        dummy = LLMResponse(
            content="",
            provider=model_cfg.provider,
            model_id=model_cfg.model_id,
            action=request.action,
            strategy=request.strategy,
        )
        await self._log(dummy, success=False, error_message=error_message)

    async def _log(
        self,
        response: LLMResponse,
        *,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """Log LLM call via structlog and optional callback."""
        if self._log_callback:
            await self._log_callback(response, success, error_message)
        logger.info(
            "llm_call_completed",
            provider=response.provider,
            model=response.model_id,
            action=response.action,
            strategy=response.strategy,
            tokens_in=response.tokens_in,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            success=success,
        )
