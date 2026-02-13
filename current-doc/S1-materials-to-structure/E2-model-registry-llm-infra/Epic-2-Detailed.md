# Epic 2: Model Registry & LLM Infrastructure

## Мета

Уніфікований інтерфейс для роботи з кількома LLM-провайдерами з гнучким роутингом. Після цього епіку — будь-який компонент системи викликає LLM через `ModelRouter`, не знаючи деталей конкретного провайдера. Роутинг, набір провайдерів і перелік завдань — розширюються без зміни коду. Кожен виклик автоматично логується для cost tracking.

## Терміни

- **Action** — що робити: `video_analysis`, `course_structuring`, `text_processing`, ... Описує тип завдання з вимогами до моделі (vision, structured_output, long_context). Перелік actions легко розширюється.
- **Provider** — хто виконує: Gemini, Anthropic, OpenAI, DeepSeek, ... Обгортка над SDK конкретного LLM. Перелік провайдерів розширюється і змінюється динамічно (вичерпаний ліміт, провайдер недоступний).
- **Routing** — action → strategies → ordered model chains. Маппінг із **стратегіями**:
  - `default` — основний ланцюжок (використовується за замовчуванням і як fallback)
  - `quality` — кращі моделі, дорожче
  - `budget` — мінімальна вартість
  - Fallback: всередині chain (модель 1 → 2 → 3) та між strategies (requested chain впав → default chain)

## Що робимо

Чотири компоненти:

1. **LLM Providers** (S1-007) ✅ — ABC `LLMProvider` з методами `complete()` та `complete_structured()`. Реалізації: `GeminiProvider`, `AnthropicProvider`, `OpenAICompatProvider` (OpenAI + DeepSeek через `base_url`). Спільні схеми `LLMRequest`/`LLMResponse`. `StructuredOutputError` для невалідного JSON. `PROVIDER_REGISTRY` dict + `create_providers(settings)` factory. Runtime enable/disable.
2. **Actions & Model Registry** (S1-008) ✅ — `config/models.yaml` з трьома секціями: `models` (5 моделей з `Capability` StrEnum та `CostPer1K`), `actions` (4 types з вимогами), `routing` (action → strategies → ordered model chains). `ModelRegistryConfig` з Pydantic-валідацією при старті: перевірка capabilities, unknown models/actions, empty chains. `get_chain(action, strategy)` → `list[ModelConfig]`. Шлях конфігу — `Settings.model_registry_path`.
3. **ModelRouter** (S1-009) ✅ — центральна точка виклику LLM. Приймає action + prompt + optional strategy, обирає ланцюжок моделей через `registry.get_chain()`, передає `model_id` провайдеру через `request.model`. Two-level fallback: within chain (модель 1 → 2) та cross-strategy (requested → default). Класифікація помилок: permanent (401, 403) → skip одразу, transient (429, 500) → retry до `max_attempts`. Cost enrichment через `ModelConfig.estimate_cost()`. `LogCallback` для S1-010. DRY: `complete()`/`complete_structured()` через спільний `_execute_with_fallback`.
4. **LLM Call Logging** (S1-010) ✅ — автоматичне збереження кожного виклику в таблицю `llm_calls` (action, provider, model, tokens, latency, cost, strategy, success/fail). Фабрика `create_model_router()` для збирання повного стеку. `task_type` перейменовано на `action`, додано `strategy`. 7 тестів.

## Для чого

ModelRouter — ключова абстракція проєкту. Усі наступні компоненти (VideoProcessor, ArchitectAgent, GuideAgent) працюють через нього, а не напряму з SDK провайдерів. Це дає:

- **Fallback**: Gemini впав → автоматично DeepSeek → Anthropic (всередині chain). Весь requested chain впав → fallback на default strategy.
- **Strategies**: той самий action з різними пріоритетами — `quality` для фінальної генерації, `budget` для чернеток.
- **Cost control**: знаємо скільки коштує кожен виклик, кожен pipeline, кожна стратегія.
- **Vendor independence**: зміна моделі або стратегії — редагування YAML, не рефакторинг коду.
- **Runtime flexibility**: вимкнути провайдер при вичерпанні ліміту — без перезапуску додатку.
- **Валідація**: routing перевіряється при старті — не дасть призначити модель без vision на action що вимагає vision.

## Контрольні точки

- [x] `router.complete("video_analysis", prompt)` — default strategy, повертає відповідь від першого доступного провайдера
- [x] `router.complete("video_analysis", prompt, strategy="quality")` — використовує quality chain
- [x] При помилці всіх моделей у requested chain — fallback на default strategy (якщо requested != default)
- [x] Кожен виклик створює запис в `llm_calls` з action/provider/model/tokens/latency/cost/strategy (S1-010)
- [x] `config/models.yaml` валідується при старті: невалідний конфіг = помилка; модель без потрібних capabilities для action = помилка
- [x] Новий провайдер = новий клас + рядок в `PROVIDER_REGISTRY`, без зміни router.py
- [x] Новий action = запис в YAML, без зміни коду
- [x] `uv run pytest tests/unit/test_llm/` — 67 тестів зелені (14 providers + 22 registry + 24 router + 7 logging)
- [x] `make check` проходить

## Залежності

- **Блокується:** Epic 1 (репо, config, DB, CI)
- **Блокує:** Epic 3 (Ingestion використовує ModelRouter для Vision LLM), Epic 4 (ArchitectAgent — основний споживач)

## Задачі

| ID | Назва | Естімейт |
|:---|:---|:---|
| S1-007 | LLM Providers | 0.5 дня |
| S1-008 | Actions & Model Registry | 0.5 дня |
| S1-009 | ModelRouter | 0.5 дня |
| S1-010 | LLM Call logging | 0.5 дня |

**Загалом: 1–2 дні**
