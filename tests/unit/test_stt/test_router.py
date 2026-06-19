"""Tests for STTRouter fallback and retry logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.stt.router import AllSTTProvidersFailedError, STTRouter
from course_supporter.stt.schemas import STTResult


def _make_result(provider: str = "test", model_id: str = "m1") -> STTResult:
    return STTResult(text="transcript", provider=provider, model_id=model_id)


def _make_model_cfg(model_id: str = "m1", provider: str = "test") -> MagicMock:
    cfg = MagicMock()
    cfg.model_id = model_id
    cfg.provider = provider
    return cfg


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


def _make_registry(chain: list[MagicMock]) -> MagicMock:
    registry = MagicMock()
    registry.get_chain.return_value = chain
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

    async def test_cost_calculated_from_cost_per_minute(self) -> None:
        """STT cost is computed from cost_per_minute when available."""
        stt_result = _make_result()
        stt_result.audio_duration_sec = 120.0  # 2 minutes
        provider = _make_provider(result=stt_result)
        cfg = _make_model_cfg()
        cfg.cost_per_minute = 0.006  # $0.006/min
        registry = _make_registry([cfg])

        router = STTRouter({"test": provider}, registry)
        result = await router.transcribe("transcribe", "/tmp/a.mp3")

        assert result.cost_usd is not None
        assert result.cost_usd == pytest.approx(0.012, abs=1e-6)  # 2 min x $0.006


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
        result_arg, error_arg = cb.call_args[0]
        assert isinstance(result_arg, STTResult)
        assert error_arg is None

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
        _, error_arg = cb.call_args[0]
        assert error_arg is not None
