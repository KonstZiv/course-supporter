[![CI](https://github.com/KonstZiv/course-supporter/actions/workflows/ci.yaml/badge.svg)](https://github.com/KonstZiv/course-supporter/actions/workflows/ci.yaml)

# Course Supporter

AI-powered system for transforming course materials into structured learning plans with automated mentoring.

**[API (live)](https://api.pythoncourse.me/docs)**

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
- libmagic (security file-type detection):
  - macOS: `brew install libmagic` (Apple Silicon may need
    `export DYLD_LIBRARY_PATH=/opt/homebrew/lib` if `import magic`
    can't find the library)
  - Debian/Ubuntu: `apt-get install libmagic1` (already in Dockerfile)

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
