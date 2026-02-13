"""Tests for ModelRouter -- strategies, fallback, retries, error classification."""

from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.registry import ModelRegistryConfig
from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMRequest, LLMResponse

# -- test helpers -------------------------------------------------------


def _resp(provider: str = "p_a", model: str = "model-a") -> LLMResponse:
    return LLMResponse(
        content="ok",
        provider=provider,
        model_id=model,
        tokens_in=10,
        tokens_out=20,
        latency_ms=100,
    )


def _registry(
    strategies: dict[str, list[str]] | None = None,
) -> ModelRegistryConfig:
    """Create a minimal registry with two models and one action."""
    return ModelRegistryConfig.model_validate(
        {
            "models": {
                "model-a": {
                    "provider": "p_a",
                    "capabilities": ["structured_output"],
                    "max_context": 100000,
                    "cost_per_1k": {"input": 0.001, "output": 0.002},
                },
                "model-b": {
                    "provider": "p_b",
                    "capabilities": ["structured_output"],
                    "max_context": 100000,
                    "cost_per_1k": {"input": 0.0001, "output": 0.0002},
                },
            },
            "actions": {
                "act": {
                    "description": "Test action",
                    "requires": ["structured_output"],
                },
            },
            "routing": {
                "act": strategies
                or {
                    "default": ["model-a", "model-b"],
                    "quality": ["model-b", "model-a"],
                    "budget": ["model-a"],
                },
            },
        }
    )


def _ok_provider(response: LLMResponse | None = None) -> LLMProvider:
    p = AsyncMock(spec=LLMProvider)
    resp = response or _resp()
    p.complete = AsyncMock(return_value=resp)
    p.complete_structured = AsyncMock(return_value=({"parsed": True}, resp))
    p.enabled = True
    return p  # type: ignore[return-value]


def _fail_provider(exc: Exception | None = None) -> LLMProvider:
    error = exc or Exception("API error")
    p = AsyncMock(spec=LLMProvider)
    p.complete = AsyncMock(side_effect=error)
    p.complete_structured = AsyncMock(side_effect=error)
    p.enabled = True
    return p  # type: ignore[return-value]


def _disabled_provider() -> LLMProvider:
    p = AsyncMock(spec=LLMProvider)
    p.enabled = False
    return p  # type: ignore[return-value]


# -- tests --------------------------------------------------------------


class TestDefaultStrategy:
    async def test_primary_model_succeeds(self) -> None:
        r = await ModelRouter(
            {"p_a": _ok_provider(_resp("p_a", "model-a")), "p_b": _ok_provider()},
            _registry(),
        ).complete("act", "hi")
        assert r.provider == "p_a"
        assert r.action == "act"
        assert r.strategy == "default"

    async def test_fallback_within_chain(self) -> None:
        r = await ModelRouter(
            {"p_a": _fail_provider(), "p_b": _ok_provider(_resp("p_b", "model-b"))},
            _registry(),
            max_attempts=1,
        ).complete("act", "hi")
        assert r.provider == "p_b"


class TestExplicitStrategy:
    async def test_quality_chain_order(self) -> None:
        """Quality chain is [model-b, model-a], so p_b is tried first."""
        r = await ModelRouter(
            {
                "p_a": _ok_provider(_resp("p_a", "model-a")),
                "p_b": _ok_provider(_resp("p_b", "model-b")),
            },
            _registry(),
        ).complete("act", "hi", strategy="quality")
        assert r.provider == "p_b"
        assert r.strategy == "quality"


class TestCrossStrategyFallback:
    async def test_quality_falls_back_to_default(self) -> None:
        """Quality chain [model-b] fails → fallback to default [model-a, model-b]."""
        reg = _registry(
            {
                "default": ["model-a", "model-b"],
                "quality": ["model-b"],
            }
        )
        r = await ModelRouter(
            {
                "p_a": _ok_provider(_resp("p_a", "model-a")),
                "p_b": _fail_provider(),
            },
            reg,
            max_attempts=1,
        ).complete("act", "hi", strategy="quality")
        assert r.provider == "p_a"
        assert "default" in r.strategy

    async def test_default_does_not_fallback_to_itself(self) -> None:
        with pytest.raises(AllModelsFailedError) as exc_info:
            await ModelRouter(
                {"p_a": _fail_provider(), "p_b": _fail_provider()},
                _registry(),
                max_attempts=1,
            ).complete("act", "hi")
        assert len(exc_info.value.strategies_tried) == 1


class TestDisabledProvider:
    async def test_skip_disabled_provider(self) -> None:
        r = await ModelRouter(
            {"p_a": _disabled_provider(), "p_b": _ok_provider(_resp("p_b", "model-b"))},
            _registry(),
        ).complete("act", "hi")
        assert r.provider == "p_b"


class TestMissingProvider:
    async def test_missing_provider_skipped(self) -> None:
        """Provider referenced in registry but not in providers dict."""
        r = await ModelRouter(
            {"p_b": _ok_provider(_resp("p_b", "model-b"))},
            _registry(),
        ).complete("act", "hi")
        assert r.provider == "p_b"


class TestAllFail:
    async def test_both_strategies_tried(self) -> None:
        with pytest.raises(AllModelsFailedError) as exc_info:
            await ModelRouter(
                {"p_a": _fail_provider(), "p_b": _fail_provider()},
                _registry(),
                max_attempts=1,
            ).complete("act", "hi", strategy="budget")
        assert "budget" in exc_info.value.strategies_tried
        assert "default" in exc_info.value.strategies_tried

    async def test_error_details_populated(self) -> None:
        with pytest.raises(AllModelsFailedError) as exc_info:
            await ModelRouter(
                {"p_a": _fail_provider(), "p_b": _fail_provider()},
                _registry(),
                max_attempts=1,
            ).complete("act", "hi")
        assert len(exc_info.value.errors) > 0


class TestCostEnrichment:
    async def test_cost_calculated_when_tokens_present(self) -> None:
        resp = _resp()
        resp.tokens_in = 1000
        resp.tokens_out = 500
        r = await ModelRouter(
            {"p_a": _ok_provider(resp)},
            _registry(),
        ).complete("act", "hi")
        assert r.cost_usd is not None
        assert r.cost_usd > 0

    async def test_cost_none_when_tokens_missing(self) -> None:
        resp = _resp()
        resp.tokens_in = None
        resp.tokens_out = None
        r = await ModelRouter(
            {"p_a": _ok_provider(resp)},
            _registry(),
        ).complete("act", "hi")
        assert r.cost_usd is None


class TestLogCallback:
    async def test_callback_called_on_success(self) -> None:
        cb = AsyncMock()
        await ModelRouter(
            {"p_a": _ok_provider()},
            _registry(),
            log_callback=cb,
        ).complete("act", "hi")
        cb.assert_called_once()

    async def test_callback_receives_success_flag(self) -> None:
        cb = AsyncMock()
        await ModelRouter(
            {"p_a": _ok_provider()},
            _registry(),
            log_callback=cb,
        ).complete("act", "hi")
        assert cb.call_args[0][1] is True


class TestRetryBehavior:
    async def test_retries_up_to_max_attempts(self) -> None:
        failing = _fail_provider()
        with pytest.raises(AllModelsFailedError):
            await ModelRouter(
                {"p_a": failing},
                _registry({"default": ["model-a"]}),
                max_attempts=3,
            ).complete("act", "hi")
        assert failing.complete.call_count == 3  # type: ignore[union-attr]

    async def test_retry_then_success(self) -> None:
        """First call fails, second succeeds."""
        p = AsyncMock(spec=LLMProvider)
        p.enabled = True
        p.complete = AsyncMock(
            side_effect=[Exception("transient"), _resp("p_a", "model-a")],
        )
        r = await ModelRouter(
            {"p_a": p},  # type: ignore[dict-item]
            _registry({"default": ["model-a"]}),
            max_attempts=2,
        ).complete("act", "hi")
        assert r.content == "ok"
        assert p.complete.call_count == 2


class TestPermanentError:
    async def test_permanent_error_skips_retries(self) -> None:
        """HTTP 401 should not be retried."""

        class FakeAuthError(Exception):
            status_code = 401

        p = _fail_provider(FakeAuthError("unauthorized"))
        with pytest.raises(AllModelsFailedError):
            await ModelRouter(
                {"p_a": p},
                _registry({"default": ["model-a"]}),
                max_attempts=3,
            ).complete("act", "hi")
        assert p.complete.call_count == 1  # type: ignore[union-attr]


class TestCompleteStructured:
    async def test_structured_success(self) -> None:
        class MySchema(BaseModel):
            name: str

        p = _ok_provider()
        _parsed, response = await ModelRouter(
            {"p_a": p},
            _registry(),
        ).complete_structured("act", "hi", MySchema)
        assert response.action == "act"
        p.complete_structured.assert_called_once()  # type: ignore[union-attr]

    async def test_structured_fallback(self) -> None:
        class MySchema(BaseModel):
            name: str

        _parsed, response = await ModelRouter(
            {"p_a": _fail_provider(), "p_b": _ok_provider(_resp("p_b", "model-b"))},
            _registry(),
            max_attempts=1,
        ).complete_structured("act", "hi", MySchema)
        assert response.provider == "p_b"


class TestModelIdPassedToProvider:
    async def test_model_set_on_request(self) -> None:
        """Router must set request.model to model_id from registry chain."""
        captured_requests: list[LLMRequest] = []

        async def capture_complete(req: LLMRequest) -> LLMResponse:
            captured_requests.append(req)
            return _resp()

        p = AsyncMock(spec=LLMProvider)
        p.enabled = True
        p.complete = AsyncMock(side_effect=capture_complete)

        await ModelRouter(
            {"p_a": p},  # type: ignore[dict-item]
            _registry({"default": ["model-a"]}),
        ).complete("act", "hi")

        assert captured_requests[0].model == "model-a"


class TestIsRetryable:
    def test_structured_output_error_is_retryable(self) -> None:
        from pydantic import ValidationError

        class _S(BaseModel):
            x: int

        try:
            _S.model_validate({"x": "not_int"})
        except ValidationError as e:
            exc = StructuredOutputError("test", "{}", "Schema", e)
            assert ModelRouter._is_retryable(exc) is True

    def test_401_is_permanent(self) -> None:
        class AuthError(Exception):
            status_code = 401

        assert ModelRouter._is_retryable(AuthError()) is False

    def test_429_is_retryable(self) -> None:
        class RateLimitError(Exception):
            status_code = 429

        assert ModelRouter._is_retryable(RateLimitError()) is True

    def test_500_is_retryable(self) -> None:
        class ServerError(Exception):
            status_code = 500

        assert ModelRouter._is_retryable(ServerError()) is True

    def test_generic_exception_is_retryable(self) -> None:
        assert ModelRouter._is_retryable(Exception("network timeout")) is True
