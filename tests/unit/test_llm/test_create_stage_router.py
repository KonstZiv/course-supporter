"""One StageRouter factory for both entry points.

``create_stage_router`` is what the API lifespan and the worker's startup
build their router with, both through ``boot.build_checked_stage_router``.
The first class pins what it builds from fake settings (dummy key strings,
no network); the other two are the lock: each entry point must call this
very function, so a private copy of the wiring in either one turns its own
test red.
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from course_supporter.config import Settings, get_settings
from course_supporter.llm.factory import create_stage_router
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.providers.deepseek import DeepSeekProvider
from course_supporter.llm.providers.deepseek_thinking import DeepSeekThinkingProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.stage_router import StageRouter
from tests._helpers.registry import empty_registry


class TestCreateStageRouter:
    """What the factory builds from settings, ladders and a registry."""

    def test_registers_a_provider_per_configured_key_and_the_ladders(self) -> None:
        s = Settings(
            gemini_api_key="test-key",  # type: ignore[arg-type]
            deepseek_api_key="test-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        ladder_config = load_ladder_config(s.ladders_dir)
        registry = empty_registry()
        session_factory = MagicMock()

        with patch(
            "course_supporter.llm.factory.StageRouter", wraps=StageRouter
        ) as router_cls:
            router = create_stage_router(
                s,
                ladder_config=ladder_config,
                registry=registry,
                session_factory=session_factory,
            )

        assert isinstance(router, StageRouter)
        router_cls.assert_called_once()
        kwargs = router_cls.call_args.kwargs
        providers = kwargs["providers"]
        # DEEPSEEK_API_KEY feeds both DeepSeek pools (config.py), so one key
        # registers the thinking-on sibling too.
        assert set(providers) == {"gemini", "deepseek", "deepseek_thinking"}
        assert isinstance(providers["gemini"], GeminiProvider)
        assert isinstance(providers["deepseek"], DeepSeekProvider)
        assert isinstance(providers["deepseek_thinking"], DeepSeekThinkingProvider)
        assert kwargs["ladder_config"] is ladder_config
        assert "safety_check" in kwargs["ladder_config"].stages
        assert kwargs["registry"] is registry
        assert kwargs["session_factory"] is session_factory
        assert kwargs["record_full_input"] is False

    def test_no_keys_builds_a_router_without_providers(self) -> None:
        s = Settings(_env_file=None)

        with patch(
            "course_supporter.llm.factory.StageRouter", wraps=StageRouter
        ) as router_cls:
            create_stage_router(
                s,
                ladder_config=load_ladder_config(s.ladders_dir),
                registry=empty_registry(),
                session_factory=MagicMock(),
            )

        assert router_cls.call_args.kwargs["providers"] == {}

    def test_full_input_recording_comes_from_settings(self) -> None:
        s = Settings(call_register_full_input=True, _env_file=None)

        with patch(
            "course_supporter.llm.factory.StageRouter", wraps=StageRouter
        ) as router_cls:
            create_stage_router(
                s,
                ladder_config=load_ladder_config(s.ladders_dir),
                registry=empty_registry(),
                session_factory=MagicMock(),
            )

        assert router_cls.call_args.kwargs["record_full_input"] is True


class TestApiLifespanUsesTheFactory:
    """The lock on the API side."""

    @pytest.mark.asyncio
    async def test_lifespan_builds_its_router_with_create_stage_router(self) -> None:
        from course_supporter.api.app import app, lifespan
        from course_supporter.config import settings
        from course_supporter.storage.database import async_session

        with (
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "course_supporter.api.app.create_pool",
                new_callable=AsyncMock,
                return_value=AsyncMock(),
            ),
            patch("course_supporter.boot.create_stage_router") as factory,
        ):
            mock_engine.dispose = AsyncMock()
            mock_s3_cls.return_value = AsyncMock()

            async with lifespan(app):
                assert app.state.stage_router is factory.return_value

        factory.assert_called_once_with(
            settings,
            ladder_config=ANY,
            registry=ANY,
            session_factory=async_session,
        )


class TestWorkerStartupUsesTheFactory:
    """The lock on the worker side."""

    async def test_startup_builds_its_router_with_create_stage_router(self) -> None:
        from course_supporter.worker import startup

        ctx: dict[str, object] = {}
        session_factory = MagicMock()
        with (
            patch("course_supporter.worker.configure_logging"),
            patch("sqlalchemy.ext.asyncio.create_async_engine"),
            patch(
                "sqlalchemy.ext.asyncio.async_sessionmaker",
                return_value=session_factory,
            ),
            patch("course_supporter.boot.create_stage_router") as factory,
        ):
            await startup(ctx)

        assert ctx["stage_router"] is factory.return_value
        factory.assert_called_once_with(
            get_settings(),
            ladder_config=ANY,
            registry=ANY,
            session_factory=session_factory,
        )
