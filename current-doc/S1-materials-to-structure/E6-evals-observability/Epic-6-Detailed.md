# Epic 6: Evaluations & Observability

## Мета

Інструменти для оцінки якості генерації та моніторингу системи. Після цього епіку — є еталонна розбивка курсу, eval-скрипт для порівняння output ArchitectAgent з еталоном, cost/latency звіт по LLM calls, та structured logging для production.

## Передумови

- **Epic 1–4 ✅**: Повний pipeline: матеріали → Ingestion → CourseContext → ArchitectAgent → CourseStructure → DB
- **Epic 5**: API endpoints для end-to-end flow

## Що робимо

П'ять задач:

1. **Test dataset** (S1-029) — підготовка тестового набору матеріалів (1 коротке відео або transcript, 1 PDF, 1 текстовий файл). Зберігається в `tests/fixtures/eval/`. Достатньо для одного повного прогону pipeline.
2. **Reference structure** (S1-030) — еталонна розбивка курсу для тестового набору. Вручну створена `CourseStructure` у JSON/YAML. Зберігається в `tests/fixtures/eval/reference_structure.json`. Це "gold standard" для оцінки якості ArchitectAgent.
3. **Eval script** (S1-031) — `scripts/eval_architect.py`: завантажує test dataset → Ingestion → ArchitectAgent → порівнює з reference. Метрики: module count match, lesson coverage, concept overlap (fuzzy). Виводить structured report.
4. **Cost report** (S1-032) — агрегація `llm_calls` таблиці. Скрипт або API endpoint: total cost, cost per action, cost per provider, avg latency. Формат: JSON + human-readable table.
5. **Structlog setup** (S1-033) — production-ready logging. `structlog` конфігурація: JSON format для production, console (dev-friendly) для development. Processors: timestamp, log level, caller info. Інтеграція з FastAPI middleware для request/response logging.

## Для чого

Без eval — неможливо об'єктивно оцінити якість ArchitectAgent і порівняти різні промпти/моделі. Без cost report — неможливо контролювати витрати. Без structured logging — неможливо дебажити production.

## Контрольні точки

- [ ] Test dataset підготовлений і доступний в `tests/fixtures/eval/`
- [ ] Reference structure описує очікуваний output для test dataset
- [ ] Eval script запускається `uv run python scripts/eval_architect.py` і виводить метрики
- [ ] Cost report агрегує дані з `llm_calls`
- [ ] Structlog виводить JSON у production, colored console у dev
- [ ] FastAPI middleware логує request/response
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 4 (ArchitectAgent), Epic 5 (API)
- **Паралелізм:** S1-029 + S1-030 можна робити паралельно. S1-033 (structlog) не залежить від інших задач Epic 6.

## Задачі

| ID | Назва | Естімейт | Примітка |
|:---|:---|:---|:---|
| S1-029 | Test dataset | 0.5 дня | Fixtures для eval |
| S1-030 | Reference structure | 0.5 дня | Gold standard JSON |
| S1-031 | Eval script | 1 день | Pipeline + comparison metrics |
| S1-032 | Cost report | 0.5 дня | LLM calls aggregation |
| S1-033 | Structlog setup | 1 день | JSON/console, FastAPI middleware |

**Загалом: ~3.5 дні**

## Ризики

- **Eval metrics** — порівняння course structures не trivial (різний порядок модулів, синоніми в назвах). Мітигація: fuzzy matching, фокус на structural similarity (кількість модулів/уроків/концепцій).
- **Reference bias** — еталон створений вручну і може бути суб'єктивним. Мітигація: кілька рецензентів (post-MVP).
- **Cost tracking accuracy** — не всі провайдери повертають точні token counts. Мітигація: оцінка через tiktoken де потрібно.
