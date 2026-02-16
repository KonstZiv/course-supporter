# PD-008: Dockerfile (multi-stage) — Detail

## Контекст

Production image для Course Supporter API. Multi-stage build для мінімального розміру.

## Dockerfile

```dockerfile
# ── Build stage ──
FROM python:3.13-slim AS builder

WORKDIR /build

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies (cached layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy application code
COPY src/ src/
COPY config/ config/
COPY prompts/ prompts/
COPY migrations/ migrations/
COPY alembic.ini .
COPY scripts/ scripts/

# ── Runtime stage ──
FROM python:3.13-slim

# System dependencies for psycopg (libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app

# Copy virtual environment and application
COPY --from=builder /build/.venv .venv/
COPY --from=builder /build/src src/
COPY --from=builder /build/config config/
COPY --from=builder /build/prompts prompts/
COPY --from=builder /build/migrations migrations/
COPY --from=builder /build/alembic.ini .
COPY --from=builder /build/scripts scripts/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "course_supporter.api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "info"]
```

## Важливі деталі

**Layer caching**: `pyproject.toml` + `uv.lock` копіюються окремо від src/ — зміна коду не перебудовує dependencies.

**uv.lock**: потрібно закомітити `uv.lock` в репозиторій для reproducible builds. Якщо його ще немає — `uv lock`.

**psycopg binary**: `psycopg[binary]` включає libpq. Для slim image потрібен `libpq5` runtime library.

**curl**: потрібен для HEALTHCHECK. Альтернатива — Python script, але curl простіший.

**Workers**: 2 workers для 2 vCPU. Не 4 — щоб залишити ресурси для PostgreSQL та інших контейнерів.

## .dockerignore

```
.git
.github
.venv
__pycache__
*.pyc
.env
.env.*
tests/
.ruff_cache
.mypy_cache
.pytest_cache
*.egg-info
docker-compose*.yaml
Makefile
.pre-commit-config.yaml
current-doc/
past-sprints-doc/
.DS_Store
htmlcov/
.coverage
.coverage.*
dist/
build/
postgres_data/
minio_data/
*.mp4
*.mp3
*.wav
```

## Тести

Ручна верифікація:

1. `docker build -t course-supporter .` — успішний build
2. `docker run --rm course-supporter whoami` → `app`
3. `docker images course-supporter` → size < 500MB
4. `docker run --rm course-supporter pip list` → no pytest, ruff, mypy

## Результати верифікації

```
docker build -t course-supporter .         → OK
docker run --rm course-supporter whoami    → app
docker images course-supporter             → 494MB (< 500MB)
docker run --rm course-supporter pip list  → no pytest, ruff, mypy
make check                                 → 385 passed
```

## Definition of Done

- [x] Dockerfile працює
- [x] .dockerignore створений
- [x] uv.lock закомічений
- [x] Non-root user
- [x] Image < 500MB (494MB)
- [x] `make check` зелений
- [x] Документ оновлений відповідно до фінальної реалізації
