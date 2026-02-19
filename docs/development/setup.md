# Development Setup

!!! info "Coming in Sprint 2, Epic 7"
    Full setup guide will be published here.

**Quick start:**

```bash
# Clone and install
git clone <repo-url>
cd course-supporter
uv sync                       # dev deps included by default (PEP 735)
uv run pre-commit install

# Start infrastructure
docker compose up -d           # PostgreSQL + MinIO

# Copy env and fill in API keys
cp .env.example .env

# Run migrations
make db-upgrade

# Run checks
make check                     # ruff + mypy + pytest

# Start API server
uv run uvicorn course_supporter.api:app --reload
```

See also: [Infrastructure](../architecture/infrastructure.md) | [Deployment](deployment.md)
