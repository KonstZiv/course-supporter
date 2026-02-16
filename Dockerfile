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
