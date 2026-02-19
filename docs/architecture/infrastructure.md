# Infrastructure

## Production Environment

| Component | Details |
|-----------|---------|
| **VPS** | 8 GB RAM, 2 vCPU (Xeon Gold 6132), 32 GB disk |
| **OS** | Ubuntu, Docker Engine |
| **Domain** | `api.pythoncourse.me` |
| **TLS** | Let's Encrypt (certbot, auto-renewal), TLSv1.2/1.3, HSTS |

## Container Architecture

```
┌─────────────────────────────────────────────────┐
│                   shared-net                     │
│                                                  │
│  ┌──────────┐   ┌──────────────────┐            │
│  │  nginx   │──▶│ course-supporter │            │
│  │ (Django  │   │      -app        │            │
│  │ compose) │   │  (FastAPI:8000)  │            │
│  └──────────┘   └────────┬─────────┘            │
│       │                  │                       │
│       │         ┌────────▼─────────┐            │
│       │         │ course-supporter │            │
│       │         │      -db         │            │
│       │         │ (pgvector:pg17)  │            │
│       │         └──────────────────┘            │
│       │                                          │
│       │         ┌──────────────────┐            │
│       └────────▶│    netdata       │            │
│                 │  (monitoring)    │            │
│                 └──────────────────┘            │
└─────────────────────────────────────────────────┘
```

- **nginx** lives in a separate Django project compose, connects via `shared-net`
- App and DB ports are **not exposed** to host — all traffic through nginx
- `resolver 127.0.0.11 valid=30s` pattern for dynamic container DNS resolution

## Services

### Application (`course-supporter-app`)

- **Image:** `python:3.13-slim` (multi-stage build)
- **Workers:** 2 uvicorn workers
- **Entry:** `python -m uvicorn course_supporter.api:app`

### Database (`course-supporter-db`)

- **Image:** `pgvector/pgvector:pg17`
- **Volume:** `pgdata-cs` (named volume)
- **Extensions:** pgvector (Vector(1536) for embeddings)

### Monitoring (`netdata`)

- Dashboard with basic auth
- Alerts → Telegram
- Three monitoring layers: Netdata (system), `/health` (app), UptimeRobot (external)

## Object Storage

- **Provider:** Backblaze B2 (S3-compatible)
- **Bucket:** `course-supporter`
- **Endpoint:** `s3.eu-central-003.backblazeb2.com`
- **Upload:** Multipart (10MB parts) for files > 50MB, constant ~10-20 MB RAM

## CI/CD

### Test Pipeline (on every push)

```
GitHub Actions → ruff check → mypy --strict → pytest (407 tests)
```

### Deploy Pipeline (manual trigger)

```
GitHub Actions → SSH to VPS → git pull → docker compose build
    → up -d → alembic upgrade head → health check
```

### Docs Pipeline (on push to main, docs/ changes)

```
GitHub Actions → uv sync --only-group docs → mkdocs gh-deploy
```

## Key Configuration

All production config via `.env.prod` (not committed). See `.env.example` for template.

!!! warning "Always use `--env-file .env.prod`"
    All `docker compose` commands on the VPS must include `--env-file .env.prod` for variable interpolation in the compose file itself.

For full deployment instructions, see [Deployment Guide](../development/deployment.md).
