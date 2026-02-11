# Epic 2: Model Registry & LLM Infrastructure

## Мета

Уніфікований інтерфейс для роботи з кількома LLM-провайдерами з гнучким роутингом. Після цього епіку — будь-який компонент системи викликає LLM через `ModelRouter`, не знаючи деталей конкретного провайдера. Роутинг, набір провайдерів і перелік завдань — розширюються без зміни коду. Кожен виклик автоматично логується для cost tracking.

## Терміни

- **Action** — що робити: `video_analysis`, `course_structuring`, `text_processing`, ... Описує тип завдання з вимогами до моделі (vision, structured_output, long_context). Перелік actions легко розширюється.
- **Provider** — хто виконує: Gemini, Anthropic, OpenAI, DeepSeek, ... Обгортка над SDK конкретного LLM. Перелік провайдерів розширюється і змінюється динамічно (вичерпаний ліміт, провайдер недоступний).
- **Routing** — action → providers. Маппінг із **стратегіями**:
  - `default` — основний ланцюжок (використовується за замовчуванням)
  - `quality` — кращі моделі, дорожче
  - `budget` — мінімальна вартість
  - Fallback: всередині chain (модель 1 → 2 → 3) та між strategies (default chain впав → alternative)

## Що робимо

Чотири компоненти:

1. **LLM Providers** (S1-007) — ABC `LLMProvider` з методами `complete()` та `complete_structured()`. Реалізації для Gemini, Anthropic, OpenAI/DeepSeek. Розширюваний registry: додати новий провайдер = написати клас + зареєструвати в конфігу. Runtime-зміни: провайдер може бути вимкнений/увімкнений без перезапуску (вичерпаний ліміт токенів, помилка API).
2. **Actions & Model Registry** (S1-008) — YAML-конфігурація з трьома секціями: `models` (доступні моделі з capabilities та вартістю), `actions` (перелік завдань з вимогами), `routing` (action → strategies → ordered lists of models). Pydantic-валідація при старті: перевірка що models у routing мають capabilities, які вимагає action.
3. **ModelRouter** (S1-009) — центральна точка виклику LLM. Приймає action + prompt + optional strategy, обирає ланцюжок моделей, обробляє retry/fallback (всередині chain та між strategies), обчислює cost. API: `router.complete("video_analysis", prompt, strategy="quality")`.
4. **LLM Call Logging** (S1-010) — автоматичне збереження кожного виклику в таблицю `llm_calls` (action, provider, model, tokens, latency, cost, strategy, success/fail). Фабрика `create_model_router()` для збирання повного стеку.

## Для чого

ModelRouter — ключова абстракція проєкту. Усі наступні компоненти (VideoProcessor, ArchitectAgent, GuideAgent) працюють через нього, а не напряму з SDK провайдерів. Це дає:

- **Fallback**: Gemini впав → автоматично DeepSeek → Anthropic (всередині chain). Весь chain впав → alternative strategy.
- **Strategies**: той самий action з різними пріоритетами — `quality` для фінальної генерації, `budget` для чернеток.
- **Cost control**: знаємо скільки коштує кожен виклик, кожен pipeline, кожна стратегія.
- **Vendor independence**: зміна моделі або стратегії — редагування YAML, не рефакторинг коду.
- **Runtime flexibility**: вимкнути провайдер при вичерпанні ліміту — без перезапуску додатку.
- **Валідація**: routing перевіряється при старті — не дасть призначити модель без vision на action що вимагає vision.

## Контрольні точки

- [ ] `router.complete("video_analysis", prompt)` — default strategy, повертає відповідь від першого доступного провайдера
- [ ] `router.complete("video_analysis", prompt, strategy="quality")` — використовує quality chain
- [ ] При помилці всіх моделей в default chain — fallback на alternative strategy (якщо налаштовано)
- [ ] Кожен виклик створює запис в `llm_calls` з action/provider/model/tokens/latency/cost/strategy
- [ ] `config/models.yaml` валідується при старті: невалідний конфіг = помилка; модель без потрібних capabilities для action = помилка
- [ ] Новий провайдер = новий клас + рядок в конфігу, без зміни router.py
- [ ] Новий action = запис в YAML, без зміни коду
- [ ] `uv run pytest tests/unit/test_llm/` — всі тести зелені
- [ ] `make check` проходить

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
