# 📋 S1-010: LLM Call Logging ✅

## Мета

Автоматичне збереження кожного LLM-виклику в таблицю `llm_calls` (action, strategy, provider, model, tokens, latency, cost, success/fail). Фабрика `create_model_router()` для збирання повного стеку в один виклик.

## Контекст

Залежить від S1-005 (DB/Alembic — таблиця `llm_calls`), S1-007 (providers), S1-008 (registry), S1-009 (router + LogCallback). Завершує LLM-інфраструктуру. Після цього таску — будь-який компонент може використовувати `create_model_router()`.

---

## Acceptance Criteria

- [x] `create_log_callback(session_factory)` повертає async callback сумісний з `LogCallback`
- [x] Callback зберігає `LLMCall` запис при success (з tokens, cost, latency)
- [x] Callback зберігає `LLMCall` запис при failure (з error_message)
- [x] `action` та `strategy` зберігаються в кожному записі
- [x] DB-помилки swallowed (logged, не raised) — logging ніколи не ламає pipeline
- [x] `create_model_router(settings, session_factory)` — one-stop factory
- [x] Unit-тести з mock session — 7 тестів

---

## Реалізація

### ORM зміни

`task_type` перейменовано на `action`, додано поле `strategy`:

```python
# storage/orm.py — LLMCall
action: Mapped[str] = mapped_column(String(100), default="")
strategy: Mapped[str] = mapped_column(String(50), default="default")
```

Alembic migration: `alter_column('llm_calls', 'task_type', new_column_name='action')` + `add_column('strategy')`.

### src/course_supporter/llm/logging.py

```python
def create_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> LogCallback:
    """Create async log callback that persists LLM calls to DB."""

    async def _log_to_db(
        response: LLMResponse,
        success: bool,
        error_message: str | None,
    ) -> None:
        record = LLMCall(
            action=response.action,
            strategy=response.strategy,
            provider=response.provider,
            model_id=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            success=success,
            error_message=error_message,
        )
        try:
            async with session_factory() as session:
                session.add(record)
                await session.commit()
        except Exception:
            logger.error("llm_call_log_failed", ...)

    return _log_to_db
```

Key decisions:
- **LogCallback signature** matches `router.py` line 20: `Callable[[LLMResponse, bool, str | None], Awaitable[None]]`
- Router always provides `LLMResponse` (creates dummy in `_log_failure`), so response is never None
- **Import `LLMCall` at top level** — no circular imports (storage.orm doesn't import llm)
- **Separate session per call** — isolated from business transactions
- **DB errors swallowed** — logged via structlog, never raised

### src/course_supporter/llm/setup.py

```python
def create_model_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    max_attempts: int = 2,
) -> ModelRouter:
    registry = load_registry(settings.model_registry_path)
    providers = create_providers(settings)
    log_callback = create_log_callback(session_factory) if session_factory else None
    return ModelRouter(
        providers=providers,
        registry=registry,
        log_callback=log_callback,
        max_attempts=max_attempts,
    )
```

Key decisions vs original spec:
- `load_registry(settings.model_registry_path)` — pass config path (spec called without args)
- `max_attempts` not `max_retries` (matches ModelRouter.__init__)
- No `fallback_strategy` parameter (doesn't exist in ModelRouter)

### src/course_supporter/llm/__init__.py

```python
from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.setup import create_model_router

__all__ = [
    "AllModelsFailedError",
    "LLMRequest",
    "LLMResponse",
    "ModelRouter",
    "create_model_router",
]
```

---

## Тести

### tests/unit/test_llm/test_logging.py — 7 тестів

**TestLogCallback (5 tests):**
- `test_success_creates_record` — verify LLMCall fields match LLMResponse
- `test_failure_creates_record` — success=False, error_message set
- `test_action_and_strategy_saved` — verify action/strategy propagation
- `test_db_error_swallowed` — session.commit raises → no exception propagated
- `test_tokens_none_handled` — response with None tokens → record with None

**TestCreateModelRouter (2 tests):**
- `test_creates_router_without_session` — no session_factory → router works, no callback
- `test_creates_router_with_session` — mock session_factory → router has callback

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

1. ✅ Оновити `storage/orm.py` — `task_type` → `action`, додати `strategy`
2. ✅ Створити Alembic migration
3. ✅ Створити `llm/logging.py`
4. ✅ Створити `llm/setup.py`
5. ✅ Оновити `llm/__init__.py`
6. ✅ Створити `tests/unit/test_llm/test_logging.py`
7. ✅ `make check` — 84 тести зелені

---

## Примітки

- **Окрема session** — callback створює свою session. Ізоляція від бізнес-транзакцій.
- **Top-level import** — `LLMCall` імпортується на рівні модуля (не всередині callback), circular imports немає.
- **`create_model_router()`** — єдиний entry point. Знає про всі шари, решта коду знає тільки ModelRouter.
- **Без session_factory** — router працює без логування. Зручно для тестів та CLI.
