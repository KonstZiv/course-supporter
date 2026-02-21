[![CI](https://github.com/KonstZiv/course-supporter/actions/workflows/ci.yaml/badge.svg)](https://github.com/KonstZiv/course-supporter/actions/workflows/ci.yaml)
[![Docs](https://github.com/KonstZiv/course-supporter/actions/workflows/docs.yml/badge.svg)](https://kostyantynzivenko.github.io/course-supporter/)

# Course Supporter

AI-powered system for transforming course materials into structured learning plans with automated mentoring.

**[Documentation](https://kostyantynzivenko.github.io/course-supporter/)** | **[API (live)](https://api.pythoncourse.me/docs)**

---

## What it does

- **Ingests** video, presentations, text, and web links
- **Processes** content via LLM-powered pipeline (Gemini, Anthropic, OpenAI, DeepSeek)
- **Generates** structured course outlines with modules, lessons, concepts, and exercises
- **Serves** results via multi-tenant REST API with API key authentication

## Quick Start

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose

### Setup

```bash
git clone https://github.com/KonstZiv/course-supporter.git
cd course-supporter
uv sync                        # dev deps included by default (PEP 735)
cp .env.example .env           # fill in your API keys
docker compose up -d           # PostgreSQL + MinIO
make db-upgrade                # run migrations
uv run uvicorn course_supporter.api:app --reload
```

### Development

```bash
make check                     # ruff + mypy + pytest (full check)
make all                       # format + full check
uv run pytest -k "test_name"   # run single test
```

## Architecture

See the [full documentation](https://kostyantynzivenko.github.io/course-supporter/) for:

- [Architecture & ERD](https://kostyantynzivenko.github.io/course-supporter/architecture/erd/)
- [Architecture Decisions](https://kostyantynzivenko.github.io/course-supporter/architecture/decisions/)
- [Sprint Roadmap](https://kostyantynzivenko.github.io/course-supporter/sprints/)
- [Deployment Guide](https://kostyantynzivenko.github.io/course-supporter/development/deployment/)
