# 🏃 Sprint 1: "Матеріали → Структура курсу"

## Мета спрінту

Побудувати повний pipeline від завантаження матеріалів курсу (будь-яка комбінація: відео, презентації, тексти, веб-посилання) до отримання структурованого плану курсу з таймкодами, слайдами, концепціями та завданнями.

## Демо-результат

API endpoint `POST /courses`, який приймає набір матеріалів і повертає JSON зі структурою курсу. Можна показати на реальному Python-туторіалі.

## Тривалість

2 тижні (10 робочих днів)

---

## Поточний стан

- **Epic 1: DONE** — merged to main, 17 тестів (9 config + 8 ORM)
- **Epic 2: DONE** — merged to main, 67 тестів (14 providers + 22 registry + 24 router + 7 logging)
- **Epic 3: DONE** — merged to main, 101 тест (11 schemas + 17 video + 11 whisper + 13 presentation + 11 text + 8 web + 13 merge + 17 repository)
- **Epic 4: DONE** — merged to main, 55 тестів (16 models + 12 prompt + 11 agent + 16 repository)
- **Total tests: 240**, `make check` зелений
- **Migrations: 3** (initial schema + action/strategy refactor + learning fields)
- **Next: Epic 5** (API Layer)

---

## Епіки та задачі

### Epic 1: Project Bootstrap ✅

Ініціалізація репозиторію, інструментів розробки, CI та локального середовища. Після цього епіку — будь-який розробник може клонувати репо, запустити `docker compose up` і мати робоче середовище з базою даних.

**Задачі:**

| ID | Назва | Статус | Опис |
| :---- | :---- | :---- | :---- |
| S1-001 | Ініціалізація репозиторію | ✅ | `uv init`, pyproject.toml, src layout (`src/course_supporter/`), .gitignore, README |
| S1-002 | Dev-інструменти та лінтинг | ✅ | ruff (E/W/F/I/N/UP/B/SIM/RUF/ASYNC/S/PTH/T20), mypy --strict, pre-commit hooks |
| S1-003 | Docker Compose середовище | ✅ | `pgvector/pgvector:pg17` + MinIO, `docker-compose.yaml` |
| S1-004 | Конфігурація додатку | ✅ | Pydantic Settings, `SecretStr` для API keys, `database_url` computed field, `.env.example`. 9 тестів |
| S1-005 | Alembic та початкова міграція | ✅ | Sync template (psycopg v3), 8 таблиць: courses, source_materials, modules, lessons, concepts, exercises, slide_video_mappings, llm_calls. UUIDv7, pgvector. 8 тестів |
| S1-006 | CI pipeline | ✅ | GitHub Actions: lint → typecheck → test → ai-review (Gemini). Python 3.13 з `.python-version` |

---

### Epic 2: Model Registry & LLM Infrastructure ✅

Уніфікований інтерфейс для роботи з 4 LLM-провайдерами з strategy-based routing. `ModelRouter` — центральна абстракція: two-level fallback, retry з класифікацією помилок, cost tracking, DB logging.

**Фінальна структура:**

```
src/course_supporter/llm/
├── __init__.py           # Public: ModelRouter, create_model_router, LLMRequest, LLMResponse
├── schemas.py            # LLMRequest, LLMResponse (Pydantic)
├── factory.py            # create_providers(settings) → dict[str, LLMProvider]
├── registry.py           # ModelRegistryConfig, load_registry(path), Capability StrEnum
├── router.py             # ModelRouter, AllModelsFailedError, LogCallback
├── logging.py            # create_log_callback(session_factory) → LogCallback
├── setup.py              # create_model_router(settings, session_factory) — one-stop factory
└── providers/
    ├── __init__.py        # PROVIDER_REGISTRY: gemini, anthropic, openai, deepseek
    ├── base.py            # LLMProvider ABC, StructuredOutputError
    ├── gemini.py          # GeminiProvider (google-genai SDK)
    ├── anthropic.py       # AnthropicProvider (anthropic SDK)
    └── openai_compat.py   # OpenAICompatProvider (openai SDK, DeepSeek via base_url)
```

**Задачі:**

| ID | Назва | Статус | Тести | Опис |
| :---- | :---- | :---- | :---- | :---- |
| S1-007 | LLM Providers | ✅ | 14 | ABC `LLMProvider` + 3 реалізації (Gemini, Anthropic, OpenAI/DeepSeek). `LLMRequest`/`LLMResponse`, `StructuredOutputError`, `PROVIDER_REGISTRY`, `create_providers()` |
| S1-008 | Actions & Model Registry | ✅ | 22 | `config/models.yaml`: 5 моделей, 4 actions, 3 стратегії. `Capability` StrEnum, `CostPer1K`. Pydantic-валідація routing при старті |
| S1-009 | ModelRouter | ✅ | 24 | Two-level fallback (within chain + cross-strategy). Permanent/transient error classification, retry до `max_attempts`, cost enrichment, `LogCallback` |
| S1-010 | LLM Call Logging | ✅ | 7 | `create_log_callback()` → DB persistence. `task_type` → `action` rename + `strategy` column. `create_model_router()` one-stop factory |

---

### Epic 3: Ingestion Engine ✅

Обробка всіх типів матеріалів курсу. Після цього епіку — система може прийняти відео, PDF/PPTX, текст або URL і перетворити кожне джерело на уніфікований `SourceDocument`.

**Фінальна структура:**

```
src/course_supporter/ingestion/
├── __init__.py           # Public exports
├── base.py               # SourceProcessor ABC, ProcessingError, UnsupportedFormatError
├── video.py              # GeminiVideoProcessor, WhisperVideoProcessor, VideoProcessor (composition)
├── presentation.py       # PresentationProcessor (PDF via fitz, PPTX via python-pptx, Vision LLM)
├── text.py               # TextProcessor (MD, DOCX, HTML, TXT → HEADING + PARAGRAPH chunks)
├── web.py                # WebProcessor (trafilatura → WEB_CONTENT chunks)
└── merge.py              # MergeStep (sort by priority, cross-reference slides ↔ video timecodes)

src/course_supporter/models/
├── source.py             # SourceType, ChunkType (StrEnum), ContentChunk, SourceDocument
└── course.py             # SlideVideoMapEntry, CourseContext

src/course_supporter/storage/
└── repositories.py       # SourceMaterialRepository (CRUD + status machine)
```

**Задачі:**

| ID | Назва | Статус | Тести | Опис |
| :---- | :---- | :---- | :---- | :---- |
| S1-011 | SourceProcessor інтерфейс | ✅ | 11 | ABC + Pydantic schemas (SourceDocument, ContentChunk, CourseContext) |
| S1-012 | VideoProcessor (primary) | ✅ | 17 | GeminiVideoProcessor + VideoProcessor composition shell |
| S1-013 | VideoProcessor (fallback) | ✅ | 11 | WhisperVideoProcessor (FFmpeg + Whisper), auto-fallback |
| S1-014 | PresentationProcessor | ✅ | 13 | PDF (PyMuPDF) + PPTX (python-pptx) + optional Vision LLM |
| S1-015 | TextProcessor | ✅ | 11 | MD/DOCX/HTML/TXT → HEADING + PARAGRAPH chunks, без LLM |
| S1-016 | WebProcessor | ✅ | 8 | trafilatura → WEB_CONTENT chunks + content snapshot |
| S1-017 | MergeStep | ✅ | 13 | Sync merge + cross-references (slides ↔ video timecodes) |
| S1-018 | SourceMaterial persistence | ✅ | 17 | Repository CRUD + status machine (pending → processing → done/error) |

---

### Epic 4: Architect Agent (Методист) ✅

AI-агент, що аналізує `CourseContext` і генерує структуру курсу. Step-based архітектура для майбутньої міграції на chain/graph orchestration.

**Фінальна структура:**

```
src/course_supporter/agents/
├── __init__.py           # Public: ArchitectAgent, PreparedPrompt, PromptData, load_prompt, format_user_prompt
├── architect.py          # ArchitectAgent (step-based: _prepare_prompts → _generate)
└── prompt_loader.py      # PromptData (Pydantic), load_prompt(path), format_user_prompt(template, context)

src/course_supporter/models/
└── course.py             # +7 output models: CourseStructure, ModuleOutput, LessonOutput, ConceptOutput,
                          #   ExerciseOutput, SlideRange, WebReference, ModuleDifficulty

prompts/architect/
└── v1.yaml               # Pedagogical system prompt + user prompt template (version: "1.0")
```

**Задачі:**

| ID | Назва | Статус | Тести | Опис |
| :---- | :---- | :---- | :---- | :---- |
| S1-019 | Pydantic-моделі output | ✅ | 16 | 7 output моделей + `ModuleDifficulty`, learning fields (goal, knowledge, skills) |
| S1-020 | System prompt v1 + prompt_loader | ✅ | 12 | `PromptData` Pydantic model, YAML loader, pedagogical prompt v1 |
| S1-021 | ArchitectAgent клас | ✅ | 11 | Step-based: `_prepare_prompts` → `_generate`, `PreparedPrompt` NamedTuple |
| S1-022 | Збереження структури курсу | ✅ | 16 | `CourseStructureRepository`, learning fields в ORM, Alembic migration |

---

### Epic 5: API Layer

REST API для взаємодії з системою.

**Задачі:**

| ID | Назва | Опис |
| :---- | :---- | :---- |
| S1-023 | FastAPI bootstrap | CORS, health check, error handling, OpenAPI docs |
| S1-024 | POST /courses | Створення курсу з матеріалами, запуск pipeline |
| S1-025 | POST /courses/{id}/materials | Додавання матеріалу, re-run pipeline |
| S1-026 | POST /courses/{id}/slide-mapping | Ручний маппінг слайдів до таймкодів |
| S1-027 | GET /courses/{id} | Повна структура курсу |
| S1-028 | GET /courses/{id}/lessons/{id} | Окремий урок з деталями |

---

### Epic 6: Evals & Observability

Інструменти для вимірювання якості та витрат.

**Задачі:**

| ID | Назва | Опис |
| :---- | :---- | :---- |
| S1-029 | Тестовий датасет | Відео + PDF + текст + веб-посилання |
| S1-030 | Еталонна розбивка | Ручне структурування для порівняння |
| S1-031 | Eval script | Порівняння output з еталоном |
| S1-032 | Cost report | Агрегація `llm_calls`: вартість pipeline по моделях |
| S1-033 | Structlog setup | Структуровані логи JSON |

---

## Залежності між епіками

```
Epic 1 (Bootstrap) ✅
  ↓
Epic 2 (Model Registry) ✅
  ↓
Epic 3 (Ingestion) ✅ ──→ Epic 4 (Architect Agent) ✅
                                  ↓
                          Epic 5 (API Layer)
                                  ↓
                       Epic 6 (Evals & Observability)
```

- **Epic 1** — DONE. Блокувало все.
- **Epic 2** — DONE. Блокувало Epic 3 та 4 (ModelRouter).
- **Epic 3** — DONE. Блокувало Epic 4 (CourseContext → ArchitectAgent).
- **Epic 4** — DONE. Step-based ArchitectAgent, 55 тестів, 3 міграції.
- **Epic 5** — наступний. Інтеграція: Ingestion → ArchitectAgent → API.
- **Epic 6** — потребує робочий pipeline (Epic 5).

---

## Технічний стек (актуальний)

| Категорія | Інструменти |
| :---- | :---- |
| Runtime | Python 3.13, src layout |
| Deps | `uv`, PEP 735 (`[dependency-groups]` for dev, `[project.optional-dependencies]` for media) |
| API | FastAPI + Pydantic v2 |
| DB | PostgreSQL 17 (`pgvector/pgvector:pg17`), psycopg v3, SQLAlchemy 2.0+ async, Alembic (sync template) |
| PKs | UUIDv7 via `uuid-utils` |
| LLM | 4 providers (Gemini, Anthropic, OpenAI, DeepSeek), ModelRouter з strategy-based fallback |
| Storage | MinIO (S3-compatible) |
| Quality | ruff, mypy --strict, pre-commit, pytest + pytest-asyncio (asyncio_mode=auto) |
| CI | GitHub Actions: lint → typecheck → test → ai-review (Gemini) |
| Logging | structlog |

---

## Definition of Done

- ✅ `POST /courses` приймає набір матеріалів і повертає структуру курсу
- ✅ Працюють усі 4 типи SourceProcessor (video, presentation, text, web)
- ✅ VideoProcessor автоматично fallback-ає на Whisper при помилці Gemini
- ✅ ModelRouter коректно обробляє fallback між моделями
- ✅ Slide-video mapping працює через ручний endpoint
- ✅ Output відповідає Pydantic-схемам (CourseStructure → Module → Lesson → Concept → Exercise)
- ✅ Кожен LLM-виклик залогований в `llm_calls` з model/tokens/cost/action/strategy
- ✅ Eval script запущений на тестовому датасеті, результати задокументовані
- ✅ Cost одного повного прогону відомий
- ✅ CI зелений (ruff + mypy + pytest)
- ✅ README описує як запустити проєкт локально

---

## Ризики спрінту

| Ризик | Ймовірність | Мітигація |
| :---- | :---- | :---- |
| Gemini File API нестабільний для великих відео | Висока | Fallback pipeline з Whisper готовий з першого дня |
| Structured output від LLM невалідний JSON | Середня | Pydantic retry + prompt iteration (ModelRouter retries) |
| Обробка PPTX з нестандартним форматуванням | Середня | Фокус на стандартних PPTX, edge cases — в backlog |
| Scope creep через 4 типи процесорів | Середня | Строгий Definition of Done, TextProcessor та WebProcessor — найпростіші, робити першими |
| API keys rate limits при тестуванні | Низька | Мокані відповіді для unit tests, реальні API тільки для eval |

---

## Що НЕ входить у Sprint 1

- Перевірка ДЗ (Guide Agent) — Sprint 2
- RAG / embeddings для семантичного пошуку — Sprint 2
- Student model та submissions — Sprint 2
- Автоматичний slide-video mapping — Backlog
- Web content auto-refresh — Backlog
- Frontend / UI — Backlog
- Background task processing (Celery/TaskIQ) — Backlog
- Authentication / authorization — Backlog
