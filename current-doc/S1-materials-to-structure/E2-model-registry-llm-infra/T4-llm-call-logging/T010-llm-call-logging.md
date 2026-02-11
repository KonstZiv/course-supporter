# 📋 S1-010: LLM Call Logging

## Мета

Автоматичне збереження кожного LLM-виклику в таблицю `llm_calls` (action, strategy, provider, model, tokens, latency, cost, success/fail). Фабрика `create_model_router()` для збирання повного стеку в один виклик.

## Контекст

Залежить від S1-005 (DB/Alembic — таблиця `llm_calls`), S1-007 (providers), S1-008 (registry), S1-009 (router + LogCallback). Завершує LLM-інфраструктуру. Після цього таску — будь-який компонент може використовувати `create_model_router()`.

---

## Acceptance Criteria

- [ ] `create_log_callback(session_factory)` повертає async callback сумісний з `LogCallback`
- [ ] Callback зберігає `LLMCall` запис при success (з tokens, cost, latency)
- [ ] Callback зберігає `LLMCall` запис при failure (з error_message)
- [ ] `action` та `strategy` зберігаються в кожному записі
- [ ] DB-помилки swallowed (logged, не raised) — logging ніколи не ламає pipeline
- [ ] `create_model_router(settings, session_factory)` — one-stop factory
- [ ] Unit-тести з mock session

---

## Реалізація

### src/course_supporter/llm/logging.py

```python
"""LLM call logging — async callback for ModelRouter.

Persists every LLM call to llm_calls table. DB errors are swallowed
(logged via structlog) to never break the main pipeline.
"""

from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.llm.schemas import LLMResponse

logger = structlog.get_logger()

LogCallback = Callable[[LLMResponse | None, Exception | None], Awaitable[None]]


def create_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> LogCallback:
    """Create async log callback that persists LLM calls to DB.

    Each call gets its own session — isolated from business transactions.
    DB errors are swallowed and logged, never raised.
    """

    async def _log_callback(
        response: LLMResponse | None,
        error: Exception | None,
    ) -> None:
        try:
            async with session_factory() as session:
                from course_supporter.db.models import LLMCall

                record = LLMCall(
                    action=response.action if response else "",
                    strategy=response.strategy if response else "default",
                    provider=response.provider if response else "unknown",
                    model_id=response.model_id if response else "unknown",
                    tokens_in=response.tokens_in if response else None,
                    tokens_out=response.tokens_out if response else None,
                    latency_ms=response.latency_ms if response else 0,
                    cost_usd=response.cost_usd if response else None,
                    success=error is None,
                    error_message=str(error) if error else None,
                )
                session.add(record)
                await session.commit()

                logger.debug(
                    "llm_call_logged",
                    action=record.action,
                    strategy=record.strategy,
                    provider=record.provider,
                    success=record.success,
                )

        except Exception as exc:
            logger.error(
                "llm_call_log_failed",
                error=str(exc),
                original_error=str(error) if error else None,
            )

    return _log_callback
```

### src/course_supporter/llm/setup.py

```python
"""One-stop factory for assembling the full LLM stack.

Usage:
    router = create_model_router(settings, session_factory)
    response = await router.complete("video_analysis", prompt)
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import Settings
from course_supporter.llm.factory import create_providers
from course_supporter.llm.logging import create_log_callback
from course_supporter.llm.registry import load_registry
from course_supporter.llm.router import ModelRouter

logger = structlog.get_logger()


def create_model_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    fallback_strategy: str | None = None,
    max_retries: int = 2,
) -> ModelRouter:
    """Assemble complete LLM stack: registry + providers + logging + router.

    Args:
        settings: application settings with API keys
        session_factory: DB session factory for logging (optional)
        fallback_strategy: cross-strategy fallback (e.g., "budget")
        max_retries: retries per model on transient errors

    Returns:
        Configured ModelRouter ready for use.
    """
    registry = load_registry()
    logger.info(
        "model_registry_loaded",
        models=len(registry.models),
        actions=len(registry.actions),
    )

    providers = create_providers(settings)

    log_callback = None
    if session_factory is not None:
        log_callback = create_log_callback(session_factory)

    router = ModelRouter(
        registry=registry,
        providers=providers,
        log_callback=log_callback,
        max_retries=max_retries,
        fallback_strategy=fallback_strategy,
    )
    logger.info(
        "model_router_created",
        providers=list(providers.keys()),
        fallback_strategy=fallback_strategy,
    )

    return router
```

### src/course_supporter/llm/__init__.py

```python
"""LLM infrastructure: providers, registry, router, logging.

Quick start:
    from course_supporter.llm import create_model_router

    router = create_model_router(settings, session_factory)
    response = await router.complete("video_analysis", prompt)
    response = await router.complete("course_structuring", prompt, strategy="quality")
"""

from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.setup import create_model_router

__all__ = [
    "AllModelsFailedError",
    "ModelRouter",
    "create_model_router",
]
```

---

## ORM модель (оновлення)

Таблиця `llm_calls` (створена в S1-005) потребує два нових поля: `action` та `strategy`.

```python
class LLMCall(Base):
    """Record of every LLM API call."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    action: Mapped[str] = mapped_column(String(100), default="")
    strategy: Mapped[str] = mapped_column(String(50), default="default")
    provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    tokens_in: Mapped[int | None] = mapped_column(default=None)
    tokens_out: Mapped[int | None] = mapped_column(default=None)
    latency_ms: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(default=None)
    success: Mapped[bool] = mapped_column(default=True)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

**Alembic migration**: додати `action` та `strategy` до існуючої таблиці.

---

## Тести

### tests/unit/test_llm/test_logging.py

```python
"""Tests for LLM call logging."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.llm.logging import create_log_callback
from course_supporter.llm.schemas import LLMResponse


class TestLogCallback:
    @pytest.mark.anyio
    async def test_logs_success(self) -> None:
        mock_session = AsyncMock()
        mock_factory = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        callback = create_log_callback(mock_factory)

        response = LLMResponse(
            content="ok",
            provider="gemini",
            model_id="gemini-2.5-flash",
            tokens_in=100,
            tokens_out=50,
            latency_ms=200,
            cost_usd=0.001,
            action="video_analysis",
            strategy="default",
        )

        await callback(response, None)
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.anyio
    async def test_logs_failure(self) -> None:
        mock_session = AsyncMock()
        mock_factory = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        callback = create_log_callback(mock_factory)

        await callback(None, RuntimeError("API down"))
        mock_session.add.assert_called_once()

    @pytest.mark.anyio
    async def test_db_error_swallowed(self) -> None:
        mock_session = AsyncMock()
        mock_factory = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.commit = AsyncMock(
            side_effect=RuntimeError("DB connection lost")
        )

        callback = create_log_callback(mock_factory)
        response = LLMResponse(content="ok", provider="test", model_id="test")

        # Should not raise
        await callback(response, None)


class TestCreateModelRouter:
    def test_creates_router(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.setup import create_model_router

        s = Settings(gemini_api_key="test-key", _env_file=None)  # type: ignore[arg-type]

        with patch(
            "course_supporter.llm.setup.load_registry"
        ) as mock_load:
            from course_supporter.llm.registry import ModelRegistryConfig

            mock_load.return_value = ModelRegistryConfig.model_validate({
                "models": {
                    "m": {
                        "provider": "gemini",
                        "capabilities": ["structured_output"],
                        "max_context": 100000,
                        "cost_per_1k": {"input": 0.001, "output": 0.002},
                    }
                },
                "actions": {
                    "test": {
                        "description": "t",
                        "requires": ["structured_output"],
                    }
                },
                "routing": {"test": {"default": ["m"]}},
            })

            router = create_model_router(s)
            assert router is not None
```

---

## Фінальна структура Epic 2

```
src/course_supporter/llm/
├── __init__.py               # Public API: ModelRouter, create_model_router
├── schemas.py                # LLMRequest, LLMResponse
├── factory.py                # create_providers()
├── registry.py               # ModelRegistryConfig, load_registry()
├── router.py                 # ModelRouter, AllModelsFailedError
├── logging.py                # create_log_callback()
├── setup.py                  # create_model_router() — one-stop factory
└── providers/
    ├── __init__.py            # PROVIDER_REGISTRY
    ├── base.py                # LLMProvider ABC + enable/disable
    ├── gemini.py
    ├── anthropic.py
    └── openai_compat.py

config/
    models.yaml               # models + actions + routing

tests/unit/test_llm/
    __init__.py
    test_providers.py
    test_registry.py
    test_router.py
    test_logging.py
```

---

## Кроки виконання

1. Оновити `db/models.py` — додати `action`, `strategy` до `LLMCall`
2. Створити Alembic migration
3. Створити `llm/logging.py`
4. Створити `llm/setup.py`
5. Створити `llm/__init__.py`
6. Створити `tests/unit/test_llm/test_logging.py`
7. `make check`

---

## Примітки

- **Окрема session** — callback створює свою session. Ізоляція від бізнес-транзакцій.
- **Import inside callback** — `from course_supporter.db.models import LLMCall` inside function, щоб уникнути circular imports.
- **`create_model_router()`** — єдиний entry point. Знає про всі шари, решта коду знає тільки ModelRouter.
- **Без session_factory** — router працює без логування. Зручно для тестів та CLI.
