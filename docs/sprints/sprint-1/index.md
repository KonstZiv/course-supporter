# Sprint 1 — Production Deploy

**Status:** Complete
**Duration:** ~2 weeks
**Tests:** 407 (56 new in Epic 1)
**Deployed:** 2026-02-18

---

## Goal

Deploy Course Supporter API to a production VPS with multi-tenant auth, rate limiting, S3 storage, and automated deploy. Result: a live `api.pythoncourse.me` with Swagger UI, ready to accept requests from tenant clients.

## Epics

### Epic 1: Multi-tenant & Auth (56 tests)

**Goal:** Build B2B API foundation — data isolation per tenant, API key authentication, rate limiting per service scope (prep/check).

| Task | Description |
|------|-------------|
| PD-001 | Tenant & API Key ORM models (`tenants` + `api_keys` tables, Alembic migration) |
| PD-002 | `tenant_id` on existing tables (`courses.tenant_id`, `llm_calls.tenant_id`) |
| PD-003 | API Key auth middleware (header → SHA-256 hash → DB lookup → tenant context) |
| PD-004 | Service scope enforcement (`prep` / `check` scopes at endpoint level) |
| PD-005 | Rate limiting middleware (in-memory sliding window per tenant + scope) |
| PD-006 | Tenant-scoped repositories (`CourseRepository` filters by `tenant_id`) |
| PD-007 | Admin CLI (`manage_tenant.py`: create tenant, issue/revoke keys) |

**Key decisions:**

- API key format: `cs_live_<32 hex chars>`, only SHA-256 hash stored in DB
- `TenantContext` frozen dataclass — `tenant_id`, `tenant_name`, `scopes`, `rate_limits`
- `Annotated` deps (`PrepDep`, `SharedDep`) for typed scope dependencies
- In-memory rate limiter (single-process); Redis planned for horizontal scaling
- `LLMCall.tenant_id` nullable — background tasks and evals have no tenant context

### Epic 2: Production Docker & Infrastructure

**Goal:** Deploy to VPS: Dockerfile, production compose, nginx routing, Backblaze B2, streaming upload up to 1GB, health checks, Netdata monitoring.

| Task | Description |
|------|-------------|
| PD-008 | Dockerfile (multi-stage: builder + runtime, python:3.13-slim, non-root user) |
| PD-009 | docker-compose.prod.yaml (app + postgres-cs + netdata, shared-net) |
| PD-010 | Nginx config for `api.pythoncourse.me` (proxy via shared-net, security headers) |
| PD-011 | SSL certificate (certbot, Let's Encrypt, auto-renewal) |
| PD-012 | Backblaze B2 integration (S3-compatible, credentials in env) |
| PD-013 | Streaming upload 1GB (S3 multipart, 10MB parts, ~10-20 MB RAM) |
| PD-014 | Deep health check (`/health` checks DB + S3, returns 200/503) |
| PD-015 | Monitoring — Netdata (dashboard with basic auth, alerts → Telegram) |

**Key decisions:**

- App/netdata ports NOT exposed to host — all traffic through nginx in shared Docker network
- `proxy_request_buffering off` — nginx streams directly to upstream
- Three monitoring layers: Netdata (system), `/health` (app), UptimeRobot (external)
- 2 uvicorn workers (not 4) to conserve RAM on shared VPS

### Epic 3: CI/CD & Hardening

**Goal:** Automated deploy via GitHub Actions, security hardening, production logging, deploy documentation, smoke test.

| Task | Description |
|------|-------------|
| PD-016 | GitHub Actions deploy workflow (SSH deploy via `appleboy/ssh-action`) |
| PD-017 | Security hardening (CORS restricted, security headers, no debug in prod) |
| PD-018 | Production logging config (JSON structured logs, structlog) |
| PD-019 | Deploy documentation (full deployment guide with troubleshooting) |
| PD-020 | Smoke test script (post-deploy: health + auth + basic CRUD) |

**Key decisions:**

- Deploy pipeline: `git pull` → `docker compose build` → `up -d` → `alembic upgrade head` (build on VPS, no Docker registry)
- Security headers in nginx: HSTS, X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy
- CORS — principle of least privilege; prod overrides via `.env.prod`

## Production Infrastructure

| Component | Details |
|-----------|---------|
| **VPS** | 8 GB RAM, 2 vCPU (Xeon Gold), 32 GB disk |
| **Domain** | `api.pythoncourse.me` |
| **TLS** | Let's Encrypt (TLSv1.2/1.3, HSTS, OCSP) |
| **Database** | PostgreSQL 17 (`pgvector/pgvector:pg17`), named volume `pgdata-cs` |
| **Object Storage** | Backblaze B2 (S3-compatible), bucket `course-supporter` |
| **Monitoring** | Netdata (dashboard + Telegram alerts), deep `/health` endpoint, UptimeRobot |
| **Deploy path** | `/opt/course-supporter` on VPS |
| **Containers** | `course-supporter-app`, `course-supporter-db`, `netdata` |

## Key Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| API key auth (not OAuth/JWT) | Simple B2B model, stateless, easy to rotate per tenant |
| SHA-256 hash storage | Raw key never stored; only hash + prefix for identification |
| In-memory rate limiter | Sufficient for single-process; Redis upgrade path clear |
| `TenantContext` via `Depends()` | Frozen dataclass injected into every endpoint, zero boilerplate |
| Shared Docker network for nginx | No port exposure, nginx resolves containers by name |
| S3 multipart upload (10MB parts) | Constant ~10-20 MB RAM for files up to 1GB |
| Build on VPS (no registry) | Simplest pipeline for single-server deployment |

## Results

- **3 epics**, 20 tasks (PD-001 – PD-020) — all complete
- **407 tests**, `make check` green
- **API live:** `https://api.pythoncourse.me/health` → status ok (DB ok, S3 ok)
- **Tenant:** `pythoncourse` created via Admin CLI
- **CI/CD:** push to main → GitHub Actions → auto-deploy → live

## Lessons Learned

1. **`--env-file .env.prod`** is a Docker Compose CLI flag for variable interpolation in the compose file itself; the `env_file:` directive only sets variables inside the container
2. **`UV_LINK_MODE=copy`** needed in Dockerfile to avoid broken symlinks when copying venv between stages
3. **Shebang issue** — uv generates scripts with shebang pointing to builder stage path; use `python -m <tool>` instead of direct binary (`uvicorn`, `alembic`, `yt-dlp`)
4. **`PYTHONPATH=/app/src`** needed for src layout when using `--no-install-project`
5. **`script_stop`** is NOT a valid param for `appleboy/ssh-action@v1`; use `set -e` in script
6. **Health check via `docker exec`** — port 8000 not exposed to host; nginx proxies via shared-net
7. **DB container recreate** — if DB container is recreated but app stays running, stale connections cause `OperationalError`; must restart app
8. **Nginx resolver pattern** — `resolver 127.0.0.11 valid=30s; set $var http://container:port;` for dynamic DNS resolution in Docker
