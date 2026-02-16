# Infrastructure Overview

## Architecture

Course Supporter API runs on a shared VPS alongside an existing Django application.
Both projects communicate through a shared Docker network and a single nginx reverse proxy.

```
                        Internet
                           │
                      :80 / :443
                           │
                    ┌──────┴──────┐
                    │    nginx    │  (Django compose)
                    │  (shared)  │
                    └──┬─────┬───┘
                       │     │
            shared-net │     │ shared-net
                       │     │
         ┌─────────────┘     └──────────────┐
         │                                   │
 ┌───────┴────────────────┐   ┌─────────────┴─────────┐
 │  Course Supporter      │   │  Django App            │
 │  compose               │   │  compose               │
 │                        │   │                        │
 │  app (:8000)           │   │  django (:8000)        │
 │  postgres-cs           │   │  postgres              │
 │  netdata (:19999)      │   │  nginx (reverse proxy) │
 └────────────────────────┘   └────────────────────────┘
```

## Components

| Component | Image | Purpose | Network |
|-----------|-------|---------|---------|
| **app** | Custom (Dockerfile) | FastAPI API server, 2 uvicorn workers | default + shared-net |
| **postgres-cs** | pgvector/pgvector:pg17 | PostgreSQL 17 with pgvector extension | default |
| **netdata** | netdata/netdata:stable | System monitoring dashboard + alerts | default + shared-net |
| **nginx** | (Django compose) | TLS termination, reverse proxy | shared-net |

## Networking

- **`default`** network: internal communication between app, postgres-cs, netdata
- **`shared-net`** (external): cross-compose routing; nginx → app, nginx → netdata
- No ports exposed on Course Supporter containers — all external traffic goes through nginx

## Domain & TLS

- **Domain:** `api.pythoncourse.me`
- **DNS:** CNAME → `pythoncourse.me` (same VPS)
- **TLS:** Let's Encrypt certificate via certbot (auto-renewal via cron)
- **Protocols:** TLSv1.2 / TLSv1.3, modern ciphers, OCSP stapling, HSTS

## Routing

| Path | Target | Auth |
|------|--------|------|
| `/` | app:8000 (FastAPI) | API key (`X-API-Key` header) |
| `/health` | app:8000 | None (public) |
| `/docs` | app:8000 (Swagger UI) | None (public) |
| `/netdata/` | netdata:19999 | HTTP basic auth |

## Storage

- **Database:** PostgreSQL 17 with pgvector, data in Docker named volume `pgdata-cs`
- **Object storage:** Backblaze B2 (S3-compatible), bucket `course-materials`
- **Dev storage:** MinIO (local S3, docker-compose.yaml)

## Key Config Files

| File | Purpose |
|------|---------|
| `docker-compose.prod.yaml` | Production services: app, postgres-cs, netdata |
| `docker-compose.yaml` | Dev services: postgres, minio, minio-init |
| `Dockerfile` | Multi-stage build: builder (uv sync) → runtime (python:3.13-slim) |
| `deploy/nginx/course-supporter.conf` | Nginx server block for api.pythoncourse.me |
| `deploy/netdata/` | Netdata config templates (Telegram alerts, custom thresholds) |
| `.env.prod` | Production env vars (NOT in git) |
| `.env.example` | Template for env vars |

## Monitoring

Three layers:

1. **Netdata** — system metrics (CPU, RAM, disk, network, Docker containers), dashboard at `/netdata/`, Telegram alerts
2. **Deep health check** — `/health` endpoint checks DB + S3 connectivity, returns 200/503
3. **UptimeRobot** (external) — pings `/health` every 5 min, Telegram alert on downtime

## Security

- Non-root Docker user (`app`) in Dockerfile
- API key authentication with SHA-256 hashing
- Tenant data isolation via `tenant_id` scoping
- Rate limiting per tenant per scope (prep/check)
- TLS hardening (modern ciphers, HSTS, OCSP)
- Security headers: X-Content-Type-Options, X-Frame-Options, X-XSS-Protection
- Netdata dashboard behind HTTP basic auth
- No Docker socket exposed to app container (only to netdata for container metrics)
