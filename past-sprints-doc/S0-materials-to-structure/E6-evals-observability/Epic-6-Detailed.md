# Epic 6: Evaluations & Observability

## Мета

Інструменти для оцінки якості генерації та моніторингу системи. Після цього епіку — є еталонна розбивка курсу, eval-скрипт для порівняння output ArchitectAgent з еталоном, cost/latency звіт по LLM calls, та structured logging для production.

## Передумови

- **Epic 1–4 ✅**: Повний pipeline: матеріали → Ingestion → CourseContext → ArchitectAgent → CourseStructure → DB
- **Epic 5 ✅**: API endpoints для end-to-end flow, 294 тести

## Рішення

- **Eval script**: real LLM за замовчуванням + `--mock` flag для CI
- **Cost report**: CLI script + API endpoint (shared logic в `LLMCallRepository`)
- **Dataset topic**: Python basics (variables, functions, loops)
- **`llm_calls.course_id`**: НЕ додаємо (потребує міграцію + зміни в ModelRouter) — TODO на Sprint 2

## Порядок та залежності

```
S1-033 (Structlog)        — незалежний, робимо першим (інфра для решти)
S1-029 (Test Dataset)  ──┐
                          ├──→ S1-031 (Eval Script)
S1-030 (Reference)  ─────┘
S1-032 (Cost Report)      — незалежний
```

**Порядок: S1-033 → S1-029 → S1-030 → S1-031 → S1-032**

---

## S1-033: Structlog Setup (~7 тестів)

Production-ready structured logging: JSON для production, colored console для development.

**Створити:**
- `src/course_supporter/logging_config.py` — `configure_logging(environment, log_level)`
- `src/course_supporter/api/middleware.py` — `RequestLoggingMiddleware`
- `tests/unit/test_logging_config.py`

**Змінити:**
- `src/course_supporter/api/app.py` — виклик `configure_logging()` в lifespan, додати middleware

### logging_config.py

```python
def configure_logging(environment: str = "development", log_level: str = "INFO") -> None:
    """Configure structlog: JSON for production, colored console for dev/testing."""
```

- Shared processors: `merge_contextvars`, `add_log_level`, `TimeStamper(fmt="iso")`, `StackInfoRenderer`, `UnicodeDecoder`
- Production (`environment == "production"`): `JSONRenderer()`
- Dev/Testing: `ConsoleRenderer()`
- Налаштовує `structlog.configure()` + root `logging.StreamHandler` з `ProcessorFormatter`

### middleware.py

```python
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log HTTP request/response with timing."""
    SKIP_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})
    # Logs: method, path, status_code, latency_ms
```

### app.py зміни

```python
from course_supporter.logging_config import configure_logging
from course_supporter.api.middleware import RequestLoggingMiddleware

# В lifespan, перед усім іншим:
configure_logging(environment=str(settings.environment), log_level=settings.log_level)

# Після створення app:
app.add_middleware(RequestLoggingMiddleware)
```

### Тести

| # | Тест | Що перевіряє |
|:--|:-----|:-------------|
| 1 | `test_configure_production_json` | JSON format output |
| 2 | `test_configure_development_console` | ConsoleRenderer |
| 3 | `test_configure_sets_log_level` | root logger level |
| 4 | `test_configure_includes_timestamp` | ISO timestamp |
| 5 | `test_middleware_logs_request` | method/path/status_code/latency_ms |
| 6 | `test_middleware_skips_health` | /health не логується |
| 7 | `test_middleware_skips_docs` | /docs не логується |

**Нюанс:** `structlog.reset_defaults()` в test fixture щоб не leak-ати між тестами.

---

## S1-029: Test Dataset (0 тестів)

Статичні fixture файли з контентом Python basics для eval pipeline.

**Створити:**
- `tests/fixtures/eval/transcript.txt` — симульований транскрипт відео (~800-1200 слів), Python basics з pseudo-timecodes (`[00:01:30]`)
- `tests/fixtures/eval/slides.txt` — симульовані слайди (~15-20 слайдів), `--- Slide N ---` формат, title + bullet points
- `tests/fixtures/eval/tutorial.md` — Markdown туторіал (~600-800 слів) з H1-H3, code blocks

### Ключове

- Все plain text (не реальне відео/PDF) — обробляємо через `TextProcessor`
- Контент: змінні + типи даних, функції (def/return/args), цикли (for/while)
- Перекриття контенту між файлами для тесту merge deduplication
- Достатньо для 3-модульної структури (Variables, Functions, Loops)

---

## S1-030: Reference Structure (~3 тести)

Hand-crafted "gold standard" `CourseStructure` для порівняння з output ArchitectAgent.

**Створити:**
- `tests/fixtures/eval/reference_structure.json` — gold standard CourseStructure JSON
- `tests/unit/test_eval_reference.py`

### Структура reference

```
CourseStructure:
  title: "Python Basics: Variables, Functions, and Loops"
  learning_goal: "..."
  expected_knowledge: [...]
  expected_skills: [...]
  modules:
    1. "Variables and Data Types" (easy)
       - 2 lessons, ~3 concepts/lesson, ~1-2 exercises/lesson
    2. "Functions" (medium)
       - 2 lessons, ~3 concepts/lesson, ~1-2 exercises/lesson
    3. "Loops and Iteration" (medium)
       - 2 lessons, ~3 concepts/lesson, ~1-2 exercises/lesson
```

### Тести

| # | Тест | Що перевіряє |
|:--|:-----|:-------------|
| 1 | `test_reference_loads_as_course_structure` | JSON → CourseStructure validates |
| 2 | `test_reference_has_expected_modules` | 3 modules з очікуваними titles |
| 3 | `test_reference_has_concepts_and_exercises` | кожен lesson має ≥1 concept і ≥1 exercise |

---

## S1-031: Eval Script (~13 тестів)

Dual-mode eval: real LLM pipeline або mock для CI. Pure comparison logic в окремому модулі.

**Створити:**
- `src/course_supporter/evals/__init__.py`
- `src/course_supporter/evals/comparator.py` — pure comparison logic (no I/O, no LLM)
- `scripts/eval_architect.py` — CLI entrypoint (replace 1-line stub)
- `tests/fixtures/eval/mock_llm_response.json` — pre-saved mock (manually crafted, трохи відрізняється від reference)
- `tests/unit/test_evals/__init__.py`
- `tests/unit/test_evals/test_comparator.py`

**Змінити:**
- `pyproject.toml` — додати `"scripts/**/*.py"` до per-file-ignores (S101, T20)

### comparator.py

```python
@dataclass
class MetricResult:
    name: str
    score: float          # 0.0 - 1.0
    expected: int | str
    actual: int | str
    details: str = ""

@dataclass
class EvalReport:
    metrics: list[MetricResult]
    overall_score: float  # weighted average
    def to_dict(self) -> dict[str, Any]: ...
    def to_table(self) -> str: ...  # human-readable ASCII table

class StructureComparator:
    WEIGHTS: ClassVar[dict[str, float]] = {
        "module_count": 0.20,
        "lesson_count": 0.25,
        "concept_coverage": 0.30,
        "exercise_count": 0.15,
        "field_completeness": 0.10,
    }

    def compare(self, generated: CourseStructure, reference: CourseStructure) -> EvalReport: ...
    def _module_count_score(self, gen, ref) -> MetricResult: ...
    def _lesson_count_score(self, gen, ref) -> MetricResult: ...
    def _concept_coverage_score(self, gen, ref) -> MetricResult: ...  # fuzzy via SequenceMatcher
    def _exercise_count_score(self, gen, ref) -> MetricResult: ...
    def _field_completeness_score(self, gen) -> MetricResult: ...

    @staticmethod
    def _fuzzy_match_titles(generated: list[str], reference: list[str], threshold: float = 0.6) -> float: ...
```

### eval_architect.py CLI

```bash
uv run python scripts/eval_architect.py                  # real LLM pipeline
uv run python scripts/eval_architect.py --mock            # mock mode (CI)
uv run python scripts/eval_architect.py --save-mock       # run real + save response for future mocks
uv run python scripts/eval_architect.py --output report.json  # save JSON report
```

Real mode pipeline: `TextProcessor(fixtures) → MergeStep → ArchitectAgent → compare with reference`

### Тести (comparator only)

| # | Тест | Що перевіряє |
|:--|:-----|:-------------|
| 1 | `test_identical_structures_score_1_0` | Same structure → overall 1.0 |
| 2 | `test_empty_generated_scores_near_0` | Empty modules → near 0 |
| 3 | `test_module_count_exact_match` | 3 vs 3 → 1.0 |
| 4 | `test_module_count_mismatch` | 2 vs 3 → < 1.0 |
| 5 | `test_lesson_count_partial_match` | Some lessons match |
| 6 | `test_concept_coverage_fuzzy_match` | Similar but not identical titles |
| 7 | `test_concept_coverage_no_match` | Completely different → 0 |
| 8 | `test_exercise_count_metric` | Exercise counts comparison |
| 9 | `test_field_completeness_all_filled` | All fields present → 1.0 |
| 10 | `test_field_completeness_missing_fields` | Missing fields → < 1.0 |
| 11 | `test_fuzzy_match_threshold` | SequenceMatcher threshold |
| 12 | `test_report_to_dict_format` | JSON serialization |
| 13 | `test_report_to_table_includes_scores` | Table output |

---

## S1-032: Cost Report (~9 тестів)

Shared `LLMCallRepository` з SQL aggregation, CLI script і API endpoint.

**Створити:**
- `src/course_supporter/models/reports.py` — `CostSummary`, `GroupedCost`, `CostReport` (Pydantic)
- `src/course_supporter/api/routes/reports.py` — `GET /api/v1/reports/cost`
- `scripts/cost_report.py` — CLI script
- `tests/unit/test_cost_report.py`

**Змінити:**
- `src/course_supporter/storage/repositories.py` — додати `LLMCallRepository`
- `src/course_supporter/api/app.py` — зареєструвати reports router

### models/reports.py

```python
class CostSummary(BaseModel):
    total_calls: int
    successful_calls: int
    failed_calls: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    avg_latency_ms: float

class GroupedCost(BaseModel):
    group: str
    calls: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    avg_latency_ms: float

class CostReport(BaseModel):
    summary: CostSummary
    by_action: list[GroupedCost]
    by_provider: list[GroupedCost]
    by_model: list[GroupedCost]
```

### LLMCallRepository (в repositories.py)

```python
class LLMCallRepository:
    def __init__(self, session: AsyncSession) -> None: ...
    async def get_summary(self) -> CostSummary: ...         # func.sum, func.count, func.avg + coalesce()
    async def get_by_action(self) -> list[GroupedCost]: ...  # GROUP BY action
    async def get_by_provider(self) -> list[GroupedCost]: ...# GROUP BY provider
    async def get_by_model(self) -> list[GroupedCost]: ...   # GROUP BY model_id
```

**Важливо:** `coalesce()` для nullable `Float`/`Integer` стовпців — повертають `None` коли немає рядків.

### routes/reports.py

```python
router = APIRouter(tags=["reports"])

@router.get("/reports/cost")
async def get_cost_report(session: SessionDep) -> CostReport:
    repo = LLMCallRepository(session)
    ...
```

### scripts/cost_report.py

```bash
uv run python scripts/cost_report.py           # ASCII table
uv run python scripts/cost_report.py --json    # JSON output
```

### Тести

| # | Тест | Що перевіряє |
|:--|:-----|:-------------|
| 1 | `test_get_summary_empty_table` | No rows → zeroes |
| 2 | `test_get_summary_with_data` | Correct aggregation |
| 3 | `test_get_by_action_groups` | GROUP BY action |
| 4 | `test_get_by_provider_groups` | GROUP BY provider |
| 5 | `test_get_by_model_groups` | GROUP BY model_id |
| 6 | `test_api_cost_report_200` | GET /api/v1/reports/cost → 200 |
| 7 | `test_api_cost_report_response_schema` | CostReport schema |
| 8 | `test_cli_table_output` | print_table format |
| 9 | `test_cli_json_output` | JSON format |

---

## Фінальна структура (очікувана)

```
src/course_supporter/
├── logging_config.py          # configure_logging() — JSON/console
├── evals/
│   ├── __init__.py
│   └── comparator.py          # StructureComparator, MetricResult, EvalReport
├── models/
│   └── reports.py             # CostSummary, GroupedCost, CostReport
├── api/
│   ├── middleware.py           # RequestLoggingMiddleware
│   └── routes/
│       └── reports.py          # GET /api/v1/reports/cost
└── storage/
    └── repositories.py        # +LLMCallRepository

scripts/
├── eval_architect.py          # Eval CLI (--mock, --save-mock, --output)
└── cost_report.py             # Cost CLI (--json)

tests/
├── fixtures/eval/
│   ├── transcript.txt         # Video transcript
│   ├── slides.txt             # Presentation slides
│   ├── tutorial.md            # Markdown tutorial
│   ├── reference_structure.json  # Gold standard
│   └── mock_llm_response.json   # Pre-saved mock
└── unit/
    ├── test_logging_config.py
    ├── test_eval_reference.py
    ├── test_cost_report.py
    └── test_evals/
        └── test_comparator.py
```

## Summary

| Task | New Files | Modified Files | Tests |
|:---|:---|:---|:---|
| S1-033 | logging_config.py, middleware.py, test_logging_config.py | app.py | ~7 |
| S1-029 | 3 fixture files | — | 0 |
| S1-030 | reference_structure.json, test_eval_reference.py | — | ~3 |
| S1-031 | evals/ pkg (2), eval_architect.py, mock fixture, test_comparator.py | pyproject.toml | ~13 |
| S1-032 | models/reports.py, routes/reports.py, cost_report.py, test_cost_report.py | repositories.py, app.py | ~9 |
| **Total** | **~14 new** | **~4 modified** | **~32** |

**After Epic 6:** 294 + ~32 = **~326 тестів**

## Контрольні точки

- [x] Structlog виводить JSON у production, colored console у dev
- [x] FastAPI middleware логує request/response (skip /health, /docs)
- [x] Test dataset (3 файли) підготовлений в `tests/fixtures/eval/`
- [x] Reference structure описує очікуваний output, валідується як `CourseStructure`
- [x] Eval script: `uv run python scripts/eval_architect.py --mock` виводить метрики (overall 0.93)
- [ ] Eval script: `uv run python scripts/eval_architect.py` запускає реальний pipeline (потребує API keys)
- [x] Cost report API: `GET /api/v1/reports/cost` → JSON з summary + by_action/provider/model
- [x] Cost report CLI: `uv run python scripts/cost_report.py` → ASCII table
- [x] `make check` проходить (326 тестів)

## Залежності

- **Блокується:** Epic 4 (ArchitectAgent) ✅, Epic 5 (API) ✅
- **Блокує:** нічого (фінальний епік Sprint 1)

## Ризики

- **Eval metrics** — порівняння course structures не trivial (різний порядок модулів, синоніми). Мітигація: fuzzy matching через `difflib.SequenceMatcher`, фокус на structural similarity.
- **Reference bias** — еталон створений вручну, суб'єктивний. Мітигація: кілька рецензентів (post-MVP).
- **Cost tracking accuracy** — не всі провайдери повертають точні token counts. Мітигація: `coalesce()` для NULL значень.
- **structlog global state** — може leak-ати між тестами. Мітигація: `structlog.reset_defaults()` в fixture.
- **BaseHTTPMiddleware** — відомі issues зі streaming. Мітигація: JSON API only, не streaming (OK для MVP).
