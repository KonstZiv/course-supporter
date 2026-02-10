# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**course-supporter** — AI-powered system for transforming course materials into structured learning plans with automated mentoring. Ingests video, presentations, text, and web links, then generates structured course outlines via LLM-powered agents.

Python 3.13+ | src layout (`src/course_supporter/`) | async-first (FastAPI + asyncpg + async LLM SDKs)

## Common Commands

```bash
# Install dependencies (dev group included by default via PEP 735)
uv sync

# Install with heavy media deps (whisper + PyTorch ~2GB)
uv sync --extra media

# Install pre-commit hooks
uv run pre-commit install

# Lint
uv run ruff check src/ tests/

# Format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check (strict mode)
uv run mypy src/

# Run all tests
uv run pytest

# Run single test file
uv run pytest tests/unit/test_config.py

# Run single test by name
uv run pytest -k "test_name"

# Tests with coverage
uv run pytest --cov --cov-report=term-missing

# Full check (lint + typecheck + tests)
make check

# Format + full check
make all

# Start infrastructure (PostgreSQL + MinIO)
docker compose up -d

# Run DB migrations
uv run alembic upgrade head

# Start API server
uv run uvicorn course_supporter.api:app --reload
```

## Architecture

### Pipeline Flow

```
Course Materials → Ingestion Engine → SourceDocuments
    → MergeStep → Unified CourseContext
    → ArchitectAgent → Structured Output via LLM (ModelRouter)
    → API → Database Persistence
```

### Key Modules

- **`ingestion/`** — Abstract `SourceProcessor` interface with implementations: `VideoProcessor` (Gemini → Whisper fallback), `PresentationProcessor` (PDF/PPTX), `TextProcessor` (MD/DOCX/HTML), `WebProcessor`. All produce unified `SourceDocument` output.
- **`llm/`** — `ModelRouter` with provider selection and fallback logic. Providers: Gemini, Anthropic, OpenAI, DeepSeek (DeepSeek uses OpenAI SDK with custom `base_url`).
- **`agents/`** — `ArchitectAgent` generates course structure via LLM structured output with Pydantic validation and retry on invalid JSON.
- **`models/`** — Pydantic schemas: `course.py` (CourseStructure, Module, Lesson, Concept, Task), `source.py` (SourceMaterial, SourceDocument), `llm.py` (LLMCall, LLMResponse).
- **`storage/`** — SQLAlchemy async ORM, repository pattern. 8 tables: courses, source_materials, slide_video_mappings, modules, lessons, concepts, tasks, llm_calls. pgvector for embeddings.
- **`api/`** — FastAPI routes for course management.
- **`config.py`** — Pydantic Settings with env vars, `SecretStr` for API keys, DB URL assembly from components.

### Supporting Directories

- **`config/models.yaml`** — Model registry (provider configs, fallback chains)
- **`prompts/architect/v1.yaml`** — Prompt templates
- **`migrations/`** — Alembic (async template with asyncpg)
- **`scripts/`** — Evaluation scripts (`eval_architect.py`)

## Code Standards

- **Linting/Formatting:** `ruff` only. Base rules: E, W, F, I, N, UP, B, SIM, RUF. Extended in S1-002: ASYNC, S, PTH, T20. No `print()` — use `structlog`.
- **Type checking:** `mypy --strict`. Pydantic plugin enabled. Targeted `ignore_missing_imports` for untyped libs (trafilatura, pptx, docx, fitz, whisper).
- **Testing:** `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`). Fixtures over classes.
- **Docstrings/comments:** English only (Google/NumPy style).
- **Logging:** `structlog` exclusively. LLM calls tracked in `llm_calls` table with cost/tokens.

## Infrastructure

- **PostgreSQL 17-Alpine** with pgvector extension
- **MinIO** — S3-compatible object storage for course materials
- **`openai-whisper`** in separate optional dependency `media` (pulls PyTorch ~2GB). Install: `uv sync --extra media`. Mock in CI.
- **Dependency management:** PEP 735 — `dev` tools in `[dependency-groups]` (included by default with `uv sync`), `media` in `[project.optional-dependencies]`.

## Environment Configuration

Copy `.env.example` → `.env`. Required keys: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, PostgreSQL and MinIO connection vars. See `.env.example` for full template.

## Sprint Documentation

Detailed task specifications live in `current-doc/`. Each `S1-0XX-*.md` file contains acceptance criteria, exact config snippets, and implementation steps for its task.
