# Sprint 0 — Materials-to-Structure MVP

**Status:** Complete
**Duration:** ~3 weeks
**Tests:** 326
**Code review score:** 8.5/10

---

## Goal

Build a complete pipeline from uploading course materials (video, presentations, text, web links) to generating a structured course plan with timecodes, slides, concepts, and exercises.

Demo: `POST /courses` on a real Python tutorial produces a full course structure.

## Epics

### Epic 1: Project Bootstrap ("Zero to `make check`")

**Goal:** After `git clone`, a developer runs `make install && make up && make check` and gets a fully working environment with DB, linting, tests, and CI. No business logic — infrastructure only.

**Deliverables:**

- `uv init` + src layout (`src/course_supporter/`)
- `ruff` + `mypy --strict` + `pre-commit` hooks
- Docker Compose: `pgvector:pg17` + MinIO
- Pydantic Settings with `SecretStr` for API keys
- Alembic (sync template, psycopg v3) with 8 tables, UUIDv7 PKs
- GitHub Actions CI: lint → typecheck → test

**Result:** 6 tasks (S1-001 – S1-006), 17 tests.

### Epic 2: Model Registry & LLM Infrastructure

**Goal:** Unified interface to 4 LLM providers (Gemini, Anthropic, OpenAI, DeepSeek) with strategy-based routing. Any component calls LLM through `ModelRouter` without knowing provider details.

**Deliverables:**

- ABC `LLMProvider` with 3 implementations (Gemini, Anthropic, OpenAI — DeepSeek uses OpenAI SDK with custom `base_url`)
- `config/models.yaml` — 5 models, 4 actions, 3 strategies
- `ModelRouter` with two-level fallback (within chain + cross-strategy) and error classification (permanent vs transient)
- `LogCallback` for automatic DB logging of every call with cost/tokens
- One-stop factory `create_model_router()`

**Result:** 4 tasks (S1-007 – S1-010), 67 tests.

### Epic 3: Ingestion Engine

**Goal:** Accept video, PDF/PPTX, text (MD/DOCX/HTML), URLs and transform each source into a unified `SourceDocument`. Multiple sources merge into `CourseContext`.

**Deliverables:**

- ABC `SourceProcessor` + Pydantic schemas (7 `ChunkType` variants)
- `VideoProcessor` — Gemini primary + Whisper fallback via composition pattern
- `PresentationProcessor` — PDF + PPTX + optional Vision LLM for slide descriptions
- `TextProcessor` — markdown/docx/html, no LLM required
- `WebProcessor` — trafilatura extraction, no LLM required
- `MergeStep` — sync cross-references (slides ↔ timecodes)
- `SourceMaterialRepository` with status machine (pending → processing → done/error)

**Result:** 8 tasks (S1-011 – S1-018), 101 tests.

### Epic 4: Architect Agent

**Goal:** AI agent that analyzes `CourseContext` and generates a structured curriculum: modules → lessons → concepts with cross-references + exercises.

**Deliverables:**

- 7 Pydantic output models (`CourseStructure`, `ModuleOutput`, `LessonOutput`, etc.) with learning-oriented fields (goal, knowledge, skills, difficulty)
- Pedagogical system prompt v1 in YAML with `PromptData` Pydantic model
- Step-based `ArchitectAgent` (`_prepare_prompts` → `_generate`)
- `CourseStructureRepository` with replace strategy (clear + cascade delete)

**Result:** 4 tasks (S1-019 – S1-022), 55 tests.

### Epic 5: API Layer

**Goal:** REST API — the system's face. Via HTTP: create course, upload materials, run Ingestion + ArchitectAgent, retrieve structure.

**Deliverables:**

- FastAPI app with lifespan (DB pool + ModelRouter + S3Client)
- 5 endpoints: `POST /courses`, `POST /materials`, `POST /slide-mapping`, `GET /courses/{id}`, `GET /lessons/{id}`
- Async `S3Client` (aiobotocore) for object storage
- Background `ingest_material` task with `PROCESSOR_MAP`
- `CourseRepository`, `SlideVideoMappingRepository`, `LessonRepository`

**Result:** 6 tasks (S1-023 – S1-028), 54 tests.

### Epic 6: Evals & Observability

**Goal:** Tools for evaluating generation quality and monitoring: structured logging, eval pipeline, cost reporting.

**Deliverables:**

- `configure_logging()` — JSON for prod, console for dev + `RequestLoggingMiddleware`
- Test dataset (Python basics: transcript, slides, tutorial)
- Reference gold standard `CourseStructure` (3 modules, 6 lessons, 13 concepts)
- `StructureComparator` (5 weighted metrics, fuzzy matching via `SequenceMatcher`)
- Dual-mode eval CLI (`--mock` for CI, real LLM by default)
- `LLMCallRepository` with SQL aggregation + cost report API endpoint

**Result:** 5 tasks (S1-029 – S1-033), 32 tests.

## Key Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| psycopg v3 only (`postgresql+psycopg://`) | Modern async driver, Alembic sync template works with both sync/async |
| UUIDv7 via `uuid-utils` | Time-ordered, sortable, no sequence bottleneck |
| PEP 735 dependency groups | `[dependency-groups]` for dev tools, `[project.optional-dependencies]` for media (Whisper + PyTorch ~2GB) |
| Strategy-based `ModelRouter` | Two-level fallback, permanent vs transient error classification |
| Composition pattern for `VideoProcessor` | Gemini + Whisper as separate classes, shell orchestrator |
| Repository `flush()` not `commit()` | Caller controls transaction boundary |
| `selectinload` chains | Avoids cartesian product (vs `joinedload`) |
| Step-based `ArchitectAgent` | Ready for LangGraph/DAG migration |

## Results

- **6 epics**, 33 tasks — all complete
- **326 tests**, `make check` green
- **3 Alembic migrations** (initial schema + action/strategy refactor + learning fields)
- **~4890 LOC** source code

## Lessons Learned

1. **Router type safety** — needs `@overload` decorators for `_execute_with_fallback` instead of `type: ignore[return-value]`
2. **`PROVIDER_CONFIG` string-based** — fragile `getattr(settings, config["key_attr"])`; refactor to `@dataclass ProviderFactoryConfig` with `Callable`
3. **`error` as terminal state** — need `error → pending` transition for retry workflow
4. **Empty `models/llm.py`** — leftover from early stage, to be removed
5. **CORS `["*"]`** — addressed in Sprint 1 (PD-017 security hardening)

## What Stayed Out of Scope

- Background task queue (deferred to Sprint 2 — ARQ + Redis)
- Integration tests with real DB
- RAG / embeddings search
- Student model, submissions
- Automatic slide-video mapping (vision-based)
- Frontend / UI
- Authentication / authorization (done in Sprint 1)
