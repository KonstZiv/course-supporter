"""Tests for LLM call logging and create_model_router factory."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from course_supporter.llm.logging import create_log_callback
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.setup import create_model_router
from course_supporter.storage.orm import LLMCall

# -- helpers ----------------------------------------------------------------


def _response(
    *,
    provider: str = "gemini",
    model_id: str = "gemini-2.5-flash",
    action: str = "course_structuring",
    strategy: str = "default",
    tokens_in: int | None = 100,
    tokens_out: int | None = 50,
    latency_ms: int = 200,
    cost_usd: float | None = 0.001,
) -> LLMResponse:
    return LLMResponse(
        content="ok",
        provider=provider,
        model_id=model_id,
        action=action,
        strategy=strategy,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )


def _mock_session_factory() -> tuple[MagicMock, AsyncMock]:
    """Create mock async session factory returning (factory, session).

    async_sessionmaker.__call__() is sync and returns an AsyncSession
    which is an async context manager.
    """
    session = AsyncMock()
    session.add = MagicMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx)

    return factory, session


# -- TestLogCallback --------------------------------------------------------


class TestLogCallback:
    async def test_success_creates_record(self) -> None:
        factory, session = _mock_session_factory()
        callback = create_log_callback(factory)

        resp = _response()
        await callback(resp, True, None)

        session.add.assert_called_once()
        record: LLMCall = session.add.call_args[0][0]
        assert isinstance(record, LLMCall)
        assert record.provider == "gemini"
        assert record.model_id == "gemini-2.5-flash"
        assert record.tokens_in == 100
        assert record.tokens_out == 50
        assert record.latency_ms == 200
        assert record.cost_usd == pytest.approx(0.001)
        assert record.success is True
        assert record.error_message is None
        session.commit.assert_called_once()

    async def test_failure_creates_record(self) -> None:
        factory, session = _mock_session_factory()
        callback = create_log_callback(factory)

        resp = _response()
        await callback(resp, False, "API rate limit exceeded")

        record: LLMCall = session.add.call_args[0][0]
        assert record.success is False
        assert record.error_message == "API rate limit exceeded"

    async def test_action_and_strategy_saved(self) -> None:
        factory, session = _mock_session_factory()
        callback = create_log_callback(factory)

        resp = _response(action="video_analysis", strategy="quality->default")
        await callback(resp, True, None)

        record: LLMCall = session.add.call_args[0][0]
        assert record.action == "video_analysis"
        assert record.strategy == "quality->default"

    async def test_db_error_swallowed(self) -> None:
        factory, session = _mock_session_factory()
        session.commit = AsyncMock(
            side_effect=OperationalError("connection lost", params=None, orig=None)
        )
        callback = create_log_callback(factory)

        resp = _response()
        # Must not raise
        await callback(resp, True, None)

    async def test_tokens_none_handled(self) -> None:
        factory, session = _mock_session_factory()
        callback = create_log_callback(factory)

        resp = _response(tokens_in=None, tokens_out=None, cost_usd=None)
        await callback(resp, True, None)

        record: LLMCall = session.add.call_args[0][0]
        assert record.tokens_in is None
        assert record.tokens_out is None
        assert record.cost_usd is None


# -- TestCreateModelRouter --------------------------------------------------


class TestCreateModelRouter:
    @patch("course_supporter.llm.setup.create_providers")
    @patch("course_supporter.llm.setup.load_registry")
    def test_creates_router_without_session(
        self,
        mock_load: MagicMock,
        mock_providers: MagicMock,
    ) -> None:
        mock_load.return_value = MagicMock()
        mock_providers.return_value = {}

        settings = MagicMock()
        settings.model_registry_path = "config/models.yaml"

        router = create_model_router(settings)

        mock_load.assert_called_once_with(settings.model_registry_path)
        mock_providers.assert_called_once_with(settings)
        assert router._log_callback is None

    @patch("course_supporter.llm.setup.create_providers")
    @patch("course_supporter.llm.setup.load_registry")
    def test_creates_router_with_session(
        self,
        mock_load: MagicMock,
        mock_providers: MagicMock,
    ) -> None:
        mock_load.return_value = MagicMock()
        mock_providers.return_value = {}

        settings = MagicMock()
        settings.model_registry_path = "config/models.yaml"
        session_factory = AsyncMock()

        router = create_model_router(settings, session_factory)

        assert router._log_callback is not None
