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
docker compose up -d           # PostgreSQL + SeaweedFS (S3) + Redis
make db-upgrade                # run migrations
uv run uvicorn course_supporter.api:app --reload
```

### Local storage (SeaweedFS)

Dev and tests use SeaweedFS as the S3-compatible store — its image is pulled
without a registry login. `docker compose up -d seaweedfs` serves S3 on
`localhost:9000` with the `S3_*` credentials from `.env` and creates
`S3_BUCKET` on every start; the service turns healthy once the bucket exists.
Data lives in the `seaweedfs_data` volume.

Coming from the old MinIO setup: SeaweedFS does not read MinIO's volume. If you
need files from it, copy them out while MinIO still runs (e.g.
`mc mirror local/course-materials ./minio-backup`), then remove the old volume:
`docker volume rm course-supporter_minio_data`.

### Development

```bash
make check                     # ruff + mypy + pytest (full check)
make all                       # format + full check
uv run pytest -k "test_name"   # run single test
```
