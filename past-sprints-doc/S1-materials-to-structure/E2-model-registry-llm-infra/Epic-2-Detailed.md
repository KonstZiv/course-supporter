# Epic 2: Model Registry & LLM Infrastructure ✅

## Мета

Уніфікований інтерфейс для роботи з кількома LLM-провайдерами з гнучким роутингом. Після цього епіку — будь-який компонент системи викликає LLM через `ModelRouter`, не знаючи деталей конкретного провайдера. Роутинг, набір провайдерів і перелік завдань — розширюються без зміни коду. Кожен виклик автоматично логується для cost tracking.

## Терміни

- **Action** — що робити: `video_analysis`, `course_structuring`, `text_processing`, `presentation_analysis`. Описує тип завдання з вимогами до моделі (vision, structured_output, long_context). Перелік actions легко розширюється в `config/models.yaml`.
- **Provider** — хто виконує: Gemini, Anthropic, OpenAI, DeepSeek. Обгортка над SDK конкретного LLM. Перелік провайдерів розширюється через `PROVIDER_REGISTRY` dict.
- **Strategy** — пріоритет моделей: `default` (основний + fallback), `quality` (кращі моделі), `budget` (мінімальна вартість). Кожен action має обов'язкову `default` стратегію.
- **Routing** — action → strategies → ordered model chains. Fallback: всередині chain (модель 1 → 2 → 3) та між strategies (requested chain впав → default chain).

## Що зроблено

Чотири компоненти:

1. **LLM Providers** (S1-007) ✅ — ABC `LLMProvider` з методами `complete()` та `complete_structured()`. Реалізації: `GeminiProvider` (google-genai SDK), `AnthropicProvider` (anthropic SDK), `OpenAICompatProvider` (openai SDK, OpenAI + DeepSeek через `base_url`). Спільні схеми `LLMRequest`/`LLMResponse`. `StructuredOutputError` для невалідного JSON. `PROVIDER_REGISTRY` dict + `create_providers(settings)` factory. Runtime enable/disable. 14 тестів.
2. **Actions & Model Registry** (S1-008) ✅ — `config/models.yaml` з трьома секціями: `models` (5 моделей з `Capability` StrEnum та `CostPer1K`), `actions` (4 types з вимогами), `routing` (action → strategies → ordered model chains). `ModelRegistryConfig` з Pydantic-валідацією при старті: перевірка capabilities, unknown models/actions, empty chains. `get_chain(action, strategy)` → `list[ModelConfig]`. Шлях конфігу — `Settings.model_registry_path`. 22 тести.
3. **ModelRouter** (S1-009) ✅ — центральна точка виклику LLM. Приймає action + prompt + optional strategy, обирає ланцюжок моделей через `registry.get_chain()`, передає `model_id` провайдеру через `request.model`. Two-level fallback: within chain (модель 1 → 2) та cross-strategy (requested → default). Класифікація помилок: permanent (400, 401, 403, 404) → skip одразу, transient (429, 500+, network) → retry до `max_attempts`. Cost enrichment через `ModelConfig.estimate_cost()`. `LogCallback` type alias. DRY: `complete()`/`complete_structured()` через спільний `_execute_with_fallback`. 24 тести.
4. **LLM Call Logging** (S1-010) ✅ — `create_log_callback(session_factory)` повертає async callback, сумісний з `LogCallback`. Зберігає кожен виклик в `llm_calls` (action, strategy, provider, model, tokens, latency, cost, success/fail). DB-помилки swallowed (`SQLAlchemyError`, `OSError`), логуються через structlog. `create_model_router(settings, session_factory)` — one-stop factory: `load_registry(path)` + `create_providers(settings)` + optional callback → `ModelRouter`. ORM: `task_type` перейменовано на `action`, додано `strategy` (Alembic migration). 7 тестів.

## Фінальна структура

```
src/course_supporter/llm/
├── __init__.py           # Public API: ModelRouter, create_model_router, LLMRequest, LLMResponse, AllModelsFailedError
├── schemas.py            # LLMRequest (prompt, system_prompt, model, temperature, max_tokens, action, strategy)
│                         # LLMResponse (content, provider, model_id, tokens_in/out, latency_ms, cost_usd, action, strategy, finished_at)
├── factory.py            # create_providers(settings) → dict[str, LLMProvider]. PROVIDER_CONFIG dict.
├── registry.py           # Capability StrEnum (vision, structured_output, long_context)
│                         # CostPer1K, ModelConfig (.estimate_cost()), ActionConfig
│                         # ModelRegistryConfig (models + actions + routing, Pydantic validation)
│                         # load_registry(config_path: Path) → ModelRegistryConfig
├── router.py             # LogCallback = Callable[[LLMResponse, bool, str | None], Awaitable[None]]
│                         # ModelRouter (providers, registry, log_callback, max_attempts)
│                         # AllModelsFailedError (action, strategies_tried, errors)
├── logging.py            # create_log_callback(async_sessionmaker) → LogCallback
├── setup.py              # create_model_router(settings, session_factory=None, *, max_attempts=2) → ModelRouter
└── providers/
    ├── __init__.py        # PROVIDER_REGISTRY = {gemini, anthropic, openai, deepseek}
    ├── base.py            # LLMProvider ABC (complete, complete_structured, enable/disable)
    │                      # StructuredOutputError, _LatencyTimer
    ├── gemini.py          # GeminiProvider (google-genai SDK)
    ├── anthropic.py       # AnthropicProvider (anthropic SDK)
    └── openai_compat.py   # OpenAICompatProvider (openai SDK, provider_name + base_url configurable)

config/
    models.yaml            # 5 models × 4 actions × 3 strategies

tests/unit/test_llm/
    test_providers.py      # 14 тестів
    test_registry.py       # 22 тести
    test_router.py         # 24 тести
    test_logging.py        # 7 тестів
```

## Models.yaml (актуальний)

**Models (5):** gemini-2.5-flash, gemini-2.5-pro, claude-sonnet, deepseek-chat, gpt-4o-mini

**Capabilities:** vision, structured_output, long_context

**Actions (4):**
| Action | Requires |
|--------|----------|
| video_analysis | vision, long_context |
| presentation_analysis | vision, structured_output |
| course_structuring | structured_output |
| text_processing | structured_output |

**Routing (strategies: default, quality, budget):**
| Action | default | quality | budget |
|--------|---------|---------|--------|
| video_analysis | flash → pro | pro → flash | flash |
| presentation_analysis | flash → gpt-4o-mini | pro → flash | — |
| course_structuring | flash → deepseek | claude → pro | deepseek |
| text_processing | deepseek → flash | claude → flash | deepseek |

## Контрольні точки

- [x] `router.complete("video_analysis", prompt)` — default strategy, повертає відповідь від першого доступного провайдера
- [x] `router.complete("video_analysis", prompt, strategy="quality")` — використовує quality chain
- [x] При помилці всіх моделей у requested chain — fallback на default strategy (якщо requested != default)
- [x] Permanent errors (401, 403, 404) — skip model одразу, без retry
- [x] Transient errors (429, 500+) — retry до max_attempts, потім next model
- [x] Cost enrichment: tokens_in/out → cost_usd через ModelConfig.estimate_cost()
- [x] Кожен виклик створює запис в `llm_calls` з action/strategy/provider/model/tokens/latency/cost/success
- [x] DB errors в logging callback swallowed (SQLAlchemyError, OSError)
- [x] `config/models.yaml` валідується при старті: невалідний конфіг = помилка
- [x] Новий провайдер = новий клас + рядок в `PROVIDER_REGISTRY`
- [x] Новий action = запис в YAML, без зміни коду
- [x] `create_model_router(settings)` без session — працює без DB logging
- [x] `create_model_router(settings, session_factory)` — з DB logging
- [x] `uv run pytest tests/unit/test_llm/` — 67 тестів зелені
- [x] `make check` проходить

## Залежності

- **Блокується:** Epic 1 (репо, config, DB, CI) ✅
- **Блокує:** Epic 3 (Ingestion: VideoProcessor та PresentationProcessor використовують ModelRouter для Vision LLM), Epic 4 (ArchitectAgent — основний споживач)

## Задачі

| ID | Назва | Статус | Тести | Естімейт |
|:---|:---|:---|:---|:---|
| S1-007 | LLM Providers | ✅ | 14 | 0.5 дня |
| S1-008 | Actions & Model Registry | ✅ | 22 | 0.5 дня |
| S1-009 | ModelRouter | ✅ | 24 | 0.5 дня |
| S1-010 | LLM Call logging | ✅ | 7 | 0.5 дня |

**Загалом: 1–2 дні** (фактично виконано)
