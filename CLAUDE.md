# course-supporter (backend) — Claude Code context

Before working in this repo, read `../CLAUDE.md` (workspace-level). The three non-negotiable rules apply here.

## What this project is — target state

Python backend service that:
- Ingests authored course materials (video, audio, presentation, text, web).
- Runs a two-pass LLM pipeline to produce **DocumentSummary** + **DocumentSegment[]** per document (pipeline details in vision §3 KD2, KD2a-d).
- Generates **NodeSummaryRaw** bottom-up and top-down across the CourseNode tree (vision §3 KD10).
- Serves editable **NodeSummaryFinal** with approve/accept-raw flow (vision §3 KD11).
- Processes homework submissions through safety → sanity → review → delivery stages (vision §3 KD15).

**Target architecture is in `../refactoring-vision/vision.md`.** The code you see in `src/` is mid-migration; it does not match vision yet.

## Commands (most-used)

```bash
# Dependencies
uv sync                              # install all deps (PEP 735 dev group included)
uv sync --extra media                # + whisper + torch (~2GB)
uv run pre-commit install            # git hooks

# Quality gates
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/                     # strict mode
make check                           # lint + typecheck + tests
make all                             # format + full check

# Tests
uv run pytest
uv run pytest tests/unit/test_foo.py
uv run pytest -k "test_name"

# Infrastructure
docker compose up -d                 # Postgres + MinIO + Redis

# DB migrations
make db-upgrade
make db-downgrade
make migrate msg="description"

# Run
uv run uvicorn course_supporter.api:app --reload
arq course_supporter.worker.WorkerSettings
```

## Rules specific to backend

### Schema changes

**Any schema change is a migration + tests + vision consistency check.**

When adding/renaming a model:
1. Confirm it matches vision §2.2 (Layer contracts) and the relevant KD.
2. Write the SQLAlchemy model.
3. `make migrate msg="<stage>-<short-name>"` — Alembic autogenerate.
4. **Review the autogenerate output** — alembic often misses enum renames, check constraints, indexes. Hand-edit the migration file if needed.
5. `make db-upgrade` locally, run tests with fresh DB.
6. Migration file is **not** final until tests pass with a clean DB.

### Legacy mapping references

In the current codebase you will find:
- `MaterialNode` → new name is **`CourseNode`**.
- `MaterialEntry` → **`AuthoredDocument`**.
- `MaterialMacroSection` → **`DocumentSummary`**.
- `MaterialSegment` → **`DocumentSegment`**.
- `StructureNode*`, `StructureSnapshot`, `StructureNodeEditable`, `ReconciliationPreview` → **DELETED** in the new model. `StructureNodeEditable` conceptually replaced by **`NodeSummaryFinal`** but the fields differ (see vision §2.2).
- `ArchitectAgent`, `ReconcilerAgent` → **DELETED**. Replaced by the two-pass Methodist pipeline in `MethodistAgent`.

Do not preserve any of the deleted entities. Do not migrate data from them silently.

### Tests

- Unit tests in `tests/unit/`, integration in `tests/integration/`.
- Markers: `requires_db`, `requires_redis` — integration only.
- For LLM-heavy code, use fixtures that return recorded responses. Do not call real LLMs in CI.

### Logging

- `structlog` only. No `print()`. No `logging` stdlib directly.
- LLM calls tracked in `ExternalServiceCall` table (see vision §3 KD5) — every call creates a row, including failed and fallback attempts.

### Prompts

Prompts live in `prompts/<agent>/<version>.yaml`. In the new model, ladder configuration references prompts via `prompt_ref` in `config/ladders_*.yaml` (see vision §3 KD16).

### LLM router

The router (`llm/` module) implements the fallback ladder per KD7 + KD16:
- **Infrastructure errors (429/503/timeout):** retry same model with exponential backoff (3-4 attempts) → fallback.
- **Structural parsing errors:** instructor-style retry with error feedback (1-2) → fallback.
- **Semantic/truncation/empty:** fallback immediately.

Every call — successful or not — creates an `ExternalServiceCall` row.

## Archived documentation

`current-doc/sprint2-docs/`, `current-doc/sprint3-docs/`, `past-sprints-doc/` describe **previous** refactor attempts. They are moved to `archive/` and **must not be used as design input**. The current design source is `../refactoring-vision/vision.md`.
