"""Tests for STTRouter fallback and retry logic."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from course_supporter.cost_kinds import CostKind
from course_supporter.llm.registry import ModelConfig, ModelPrices
from course_supporter.stt.router import (
    AllSTTProvidersFailedError,
    STTAttempt,
    STTRouter,
)
from course_supporter.stt.schemas import STTResult


def _make_result(provider: str = "test", model_id: str = "m1") -> STTResult:
    return STTResult(text="transcript", provider=provider, model_id=model_id)


def _make_model_cfg(
    model_id: str = "m1",
    provider: str = "test",
    *,
    per_minute: str | None = None,
    granularity: int | None = None,
) -> ModelConfig:
    return ModelConfig(
        model_id=model_id,
        provider=provider,
        unit_type="minutes",
        prices=ModelPrices(per_minute=Decimal(per_minute) if per_minute else None),
        billing_granularity_sec=granularity,
    )


def _make_provider(
    result: STTResult | None = None,
    error: Exception | None = None,
) -> MagicMock:
    p = MagicMock()
    p.enabled = True
    if error:
        p.transcribe = AsyncMock(side_effect=error)
    else:
        p.transcribe = AsyncMock(return_value=result or _make_result())
    return p


def _make_registry(chain: list[ModelConfig]) -> MagicMock:
    registry = MagicMock()
    registry.get_chain.return_value = chain
    registry.catalog_version = "test-catalog"
    return registry


class TestSTTRouterHappyPath:
    async def test_primary_provider_succeeds(self) -> None:
        provider = _make_provider()
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3")

        assert result.text == "transcript"
        provider.transcribe.assert_awaited_once()

    async def test_threads_duration_sec_into_request(self) -> None:
        """duration_sec is passed through to the provider request (task M1)."""
        provider = _make_provider()
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        await router.transcribe("transcribe", "/tmp/a.mp3", duration_sec=9000.0)

        request = provider.transcribe.await_args.args[0]
        assert request.duration_sec == 9000.0

    async def test_enriches_action_and_strategy(self) -> None:
        provider = _make_provider()
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3", strategy="quality")

        assert result.action == "transcribe"
        assert result.strategy == "quality"

    async def test_cost_comes_from_the_duration_known_before_the_call(self) -> None:
        """BIL-C, decision 6: the provider's reported duration is not used.

        Lock: red if the price is taken from ``audio_duration_sec`` again.
        """
        stt_result = _make_result()
        stt_result.audio_duration_sec = 999.0  # what the provider says back
        provider = _make_provider(result=stt_result)
        cfg = _make_model_cfg(per_minute="0.006", granularity=1)
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3", duration_sec=120.0)

        assert result.cost_usd == pytest.approx(0.012)  # 2 min x $0.006

    async def test_unknown_duration_is_an_unknown_cost(self) -> None:
        provider = _make_provider()
        cfg = _make_model_cfg(per_minute="0.006", granularity=1)
        cb = AsyncMock()

        router = STTRouter({"test": provider}, _make_registry([cfg]), log_callback=cb)
        result = await router.transcribe("transcribe", "/tmp/a.mp3")

        assert result.cost_usd is None
        attempt: STTAttempt = cb.call_args.args[0]
        assert attempt.priced.cost_kind is CostKind.UNKNOWN


class TestSTTRouterFallback:
    async def test_fallback_within_chain(self) -> None:
        """Second model succeeds after first fails."""
        bad = _make_provider(error=RuntimeError("fail"))
        good = _make_provider(_make_result(provider="good"))

        cfg1 = _make_model_cfg(model_id="m1", provider="bad")
        cfg2 = _make_model_cfg(model_id="m2", provider="good")
        registry = _make_registry([cfg1, cfg2])

        router = STTRouter({"bad": bad, "good": good}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3")

        assert result.provider == "good"

    async def test_strategy_fallback_to_default(self) -> None:
        """When quality strategy fails, falls back to default."""
        provider = _make_provider()
        cfg = _make_model_cfg()
        registry = MagicMock()
        # First call (quality) returns empty chain, second (default) returns chain
        registry.get_chain.side_effect = [[], [cfg]]

        router = STTRouter({"test": provider}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3", strategy="quality")

        assert result.text == "transcript"
        assert result.strategy == "quality->default"

    async def test_default_does_not_fallback_to_itself(self) -> None:
        """Default strategy doesn't retry as fallback."""
        registry = _make_registry([])

        router = STTRouter({}, registry)
        with pytest.raises(AllSTTProvidersFailedError) as exc_info:
            await router.transcribe("transcribe", "/tmp/a.mp3")

        assert exc_info.value.strategies_tried == ["default"]


class TestSTTRouterSkips:
    async def test_skip_disabled_provider(self) -> None:
        provider = _make_provider()
        provider.enabled = False
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        with pytest.raises(AllSTTProvidersFailedError):
            await router.transcribe("transcribe", "/tmp/a.mp3")

    async def test_skip_missing_provider(self) -> None:
        cfg = _make_model_cfg(provider="missing")
        registry = _make_registry([cfg])

        router = STTRouter({}, registry)
        with pytest.raises(AllSTTProvidersFailedError):
            await router.transcribe("transcribe", "/tmp/a.mp3")


class TestSTTRouterRetry:
    async def test_retries_transient_error(self) -> None:
        """Retries on 429 (rate limit)."""
        err = MagicMock()
        err.status_code = 429
        err.__str__ = lambda self: "rate limit"

        provider = _make_provider()
        err_cls = type("Err", (Exception,), {"status_code": 429})
        provider.transcribe = AsyncMock(side_effect=[err_cls(), _make_result()])
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry, max_attempts=2)
        result = await router.transcribe("transcribe", "/tmp/a.mp3")

        assert result.text == "transcript"
        assert provider.transcribe.await_count == 2

    async def test_permanent_error_skips_retries(self) -> None:
        """401 (auth) is permanent — no retries, move to next model."""
        provider = _make_provider(
            error=type("Err", (Exception,), {"status_code": 401})()
        )
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry, max_attempts=3)
        with pytest.raises(AllSTTProvidersFailedError):
            await router.transcribe("transcribe", "/tmp/a.mp3")

        # Only 1 attempt — no retry for permanent errors.
        assert provider.transcribe.await_count == 1


class TestSTTRouterAllFail:
    async def test_all_providers_fail(self) -> None:
        provider = _make_provider(error=RuntimeError("boom"))
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry, max_attempts=1)
        with pytest.raises(AllSTTProvidersFailedError) as exc_info:
            await router.transcribe("transcribe", "/tmp/a.mp3")

        assert "boom" in str(exc_info.value)
        assert exc_info.value.action == "transcribe"


class TestSTTRouterLogCallback:
    async def test_log_callback_called_on_success(self) -> None:
        provider = _make_provider()
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])
        cb = AsyncMock()

        router = STTRouter({"test": provider}, registry, log_callback=cb)
        await router.transcribe("transcribe", "/tmp/a.mp3")

        cb.assert_awaited_once()
        attempt = cb.call_args.args[0]
        assert isinstance(attempt, STTAttempt)
        assert attempt.error_message is None
        assert attempt.provider == "test"

    async def test_log_callback_called_on_failure(self) -> None:
        provider = _make_provider(error=RuntimeError("fail"))
        cfg = _make_model_cfg()
        registry = _make_registry([cfg])
        cb = AsyncMock()

        router = STTRouter(
            {"test": provider}, registry, log_callback=cb, max_attempts=1
        )
        with pytest.raises(AllSTTProvidersFailedError):
            await router.transcribe("transcribe", "/tmp/a.mp3")

        cb.assert_awaited_once()
        attempt = cb.call_args.args[0]
        assert attempt.error_message is not None

    async def test_every_retried_attempt_is_written(self) -> None:
        """BIL-C: a retried failure may have been billed, so it has a row.

        Lock: red if only the last attempt is written again.
        """
        request = httpx.Request("POST", "https://api.example/v1")
        timeout = httpx.ReadTimeout("slow", request=request)
        provider = _make_provider(error=timeout)
        cfg = _make_model_cfg(per_minute="0.006", granularity=1)
        cb = AsyncMock()

        router = STTRouter(
            {"test": provider}, _make_registry([cfg]), log_callback=cb, max_attempts=2
        )
        with pytest.raises(AllSTTProvidersFailedError):
            await router.transcribe("transcribe", "/tmp/a.mp3", duration_sec=60.0)

        attempts = [call.args[0] for call in cb.call_args_list]
        assert len(attempts) == 2
        for attempt in attempts:
            assert attempt.error_message is not None
            assert attempt.priced.cost_kind is CostKind.ESTIMATED
            assert attempt.priced.cost_usd == Decimal("0.006000000000")
