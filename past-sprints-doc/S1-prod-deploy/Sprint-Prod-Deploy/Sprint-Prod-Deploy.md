# 🚀 Sprint: Production Deploy

## Мета спрінту

Розгорнути Course Supporter API на production VPS з multi-tenant auth, rate limiting per service scope, Backblaze B2 storage та automated deploy. Після спрінту — робочий `api.pythoncourse.me` з Swagger UI, готовий приймати запити від tenant-клієнтів.

## Демо-результат

```bash
curl -H "X-API-Key: cs_live_..." https://api.pythoncourse.me/health
# {"status": "ok", "db": "ok", "s3": "ok"}

curl -H "X-API-Key: cs_live_..." https://api.pythoncourse.me/api/v1/courses
# 200 OK (scoped to tenant)
```

Push to `main` → GitHub Actions → auto-deploy → live.

## Тривалість

3-4 дні (20 задач)

---

## Контекст інфраструктури

- **VPS:** Aviti (Україна), 3.82 GB RAM, Ubuntu
- **Існуючий стек:** Django + nginx + certbot на `pythoncourse.me`, все в Docker
- **Мережа:** `shared-net` для cross-compose routing через nginx
- **CI:** GitHub Actions (lint → typecheck → test), GitLab Runner на сервері — для іншого проєкту
- **Domain:** `api.pythoncourse.me` (новий subdomain)

---

## Епіки та задачі

### Epic 1: Multi-tenant & Auth (7 задач)

Фундамент для B2B API: ізоляція даних per tenant, API key auth, rate limiting per service scope (prep/check).

| ID | Назва | Опис |
| :---- | :---- | :---- |
| PD-001 | Tenant & API Key ORM models | Таблиці `tenants` + `api_keys`, Alembic міграція |
| PD-002 | tenant_id на існуючих таблицях | `courses.tenant_id`, `llm_calls.tenant_id`, міграція |
| PD-003 | API Key auth middleware | FastAPI dependency: header → lookup → tenant context |
| PD-004 | Service scope enforcement | `prep` / `check` scopes на endpoint-level |
| PD-005 | Rate limiting middleware | Per-tenant, per-scope limits. In-memory (sliding window) |
| PD-006 | Tenant-scoped repositories | `CourseRepository` фільтрує по `tenant_id` |
| PD-007 | Admin CLI для управління tenants | `scripts/manage_tenant.py`: create tenant, issue/revoke keys |

---

### Epic 2: Production Docker & Infrastructure (8 задач)

Dockerfile, production compose, nginx routing, Backblaze B2, streaming upload, health checks, Netdata monitoring.

| ID | Назва | Опис |
| :---- | :---- | :---- |
| PD-008 | Dockerfile (multi-stage) | Builder + runtime, slim image, non-root user |
| PD-009 | docker-compose.prod.yaml | App + PostgreSQL, `shared-net`, env_file, restart policies |
| PD-010 | Nginx config для subdomain | `api.pythoncourse.me` → upstream Course Supporter |
| PD-011 | SSL certificate | certbot для `api.pythoncourse.me` |
| PD-012 | Backblaze B2 integration | Credentials в env, перевірка з реальним bucket |
| PD-013 | Streaming upload (1GB) | Chunked read + S3 multipart upload, без тримання файлу в RAM |
| PD-014 | Deep health check | `/health` перевіряє DB connectivity + S3 reachability |
| PD-015 | Monitoring (Netdata) | Netdata контейнер, dashboard, alerts (disk/RAM/CPU) → Telegram |

---

### Epic 3: CI/CD & Hardening (5 задач)

Automated deploy, security headers, production logging, документація.

| ID | Назва | Опис |
| :---- | :---- | :---- |
| PD-016 | GitHub Actions deploy workflow | On push to main: build → push image → SSH deploy |
| PD-017 | Security hardening | CORS restricted, security headers, no debug in prod |
| PD-018 | Production logging config | JSON format, file rotation або stdout для Docker |
| PD-019 | Deploy documentation | README: як розгорнути, env vars, troubleshooting |
| PD-020 | Smoke test script | Post-deploy verification: health + auth + basic CRUD |

---

## Залежності між епіками

```
Epic 1 (Auth) ─────→ Epic 2 (Docker & Infra) ─────→ Epic 3 (CI/CD)
   PD-001..007          PD-008..015                    PD-016..020
```

Epic 1 можна паралелити з початком Epic 2 (Dockerfile не залежить від auth).

---

## Деталі ключових рішень

### Multi-tenant Data Model

```
tenants
├── id: UUID (PK)
├── name: str (Company A, Company B)
├── is_active: bool
├── created_at: datetime
└── updated_at: datetime

api_keys
├── id: UUID (PK)
├── tenant_id: UUID (FK → tenants)
├── key_hash: str (SHA-256, indexed, unique)
├── key_prefix: str ("cs_live_abc1" — для ідентифікації в логах)
├── label: str ("production", "staging", "john-testing")
├── scopes: list[str] (["prep", "check"] або ["prep"])
├── rate_limit_prep: int (requests per minute, default 60)
├── rate_limit_check: int (requests per minute, default 300)
├── is_active: bool
├── expires_at: datetime | None
└── created_at: datetime
```

### API Key Format

```
cs_live_abc12345xxxxxxxxxxxxxxxxxxxx
│  │    │
│  │    └── 32 chars random (secrets.token_hex)
│  └── environment (live/test)
│
└── prefix "cs_" (Course Supporter)
```

Зберігаємо тільки hash. Повний ключ показується тільки при створенні.

### Auth Flow

```
Request → X-API-Key header
  → SHA-256 hash
  → DB lookup api_keys WHERE key_hash AND is_active AND not expired
  → Load tenant (is_active check)
  → Check scope matches endpoint
  → Check rate limit per scope
  → Inject tenant_id into request state
  → Endpoint uses tenant_id for data isolation
```

### Rate Limiting

In-memory sliding window per (tenant_id, scope). Зберігається в dict з timestamps.
Достатньо для single-instance. При масштабуванні — Redis.

```python
# Defaults per scope (overridable per api_key):
RATE_LIMITS = {
    "prep": 60,     # requests/minute — рідкісні, тяжкі
    "check": 300,   # requests/minute — часті, легші
}
```

### Endpoint → Scope Mapping

```python
# prep scope:
POST   /api/v1/courses
POST   /api/v1/courses/{id}/materials
POST   /api/v1/courses/{id}/slide-mapping
GET    /api/v1/courses/{id}  # detail with structure

# check scope (Sprint 2, але routing готовий):
POST   /api/v1/courses/{id}/check-homework
GET    /api/v1/students/{id}/progress

# both scopes:
GET    /health
GET    /api/v1/reports/cost  (filtered by tenant)
GET    /api/v1/courses/{id}/lessons/{id}
```

### Docker Architecture on VPS

```
┌─────────────────────────────────────────────────┐
│ VPS (Aviti, 4 GB RAM)                           │
│                                                  │
│  ┌─── Django Compose ──────────────────────┐    │
│  │  django ── postgres ── nginx            │    │
│  │                          │ (shared-net)  │    │
│  └──────────────────────────┼──────────────┘    │
│                             │                    │
│  ┌─── Course Supporter ─────┼──────────────┐    │
│  │  app ── postgres-cs      │              │    │
│  │          │         (shared-net)          │    │
│  └──────────┼──────────────────────────────┘    │
│             │                                    │
│  Internet ←─┤ :80/:443                          │
│             │  api.pythoncourse.me → app:8000    │
│             │  pythoncourse.me → django:8000     │
└─────────────────────────────────────────────────┘
```

### Nginx Config (додається до існуючого)

```nginx
upstream course_supporter {
    server app:8000;  # container name in shared-net
}

upstream netdata_backend {
    server netdata:19999;  # Netdata default port
}

server {
    listen 443 ssl;
    server_name api.pythoncourse.me;

    ssl_certificate /etc/letsencrypt/live/api.pythoncourse.me/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.pythoncourse.me/privkey.pem;

    location / {
        proxy_pass http://course_supporter;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Security
        proxy_hide_header X-Powered-By;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
    }

    # Netdata dashboard (basic auth protected)
    location /netdata/ {
        auth_basic "Monitoring";
        auth_basic_user_file /etc/nginx/.htpasswd_netdata;
        proxy_pass http://netdata_backend/;
        proxy_set_header Host $host;
    }

    # Larger body for file uploads (video, presentations)
    client_max_body_size 1G;

    # Timeouts for large uploads (1GB @ 10Mbps ≈ 14 min)
    client_body_timeout 900s;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
    proxy_request_buffering off;  # Stream directly to upstream, don't buffer on disk
}

server {
    listen 80;
    server_name api.pythoncourse.me;
    return 301 https://$host$request_uri;
}
```

### Dockerfile

```dockerfile
# ── Build stage ──
FROM python:3.13-slim AS builder

WORKDIR /build
RUN pip install uv

COPY pyproject.toml .python-version ./
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY config/ config/
COPY prompts/ prompts/
COPY migrations/ migrations/
COPY alembic.ini .

# ── Runtime stage ──
FROM python:3.13-slim

RUN groupadd -r app && useradd -r -g app app
WORKDIR /app

COPY --from=builder /build/.venv .venv/
COPY --from=builder /build/src src/
COPY --from=builder /build/config config/
COPY --from=builder /build/prompts prompts/
COPY --from=builder /build/migrations migrations/
COPY --from=builder /build/alembic.ini .

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

USER app

HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

EXPOSE 8000

CMD ["uvicorn", "course_supporter.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### docker-compose.prod.yaml

```yaml
services:
  app:
    build: .
    container_name: course-supporter-app
    restart: unless-stopped
    env_file: .env.prod
    depends_on:
      postgres-cs:
        condition: service_healthy
    networks:
      - default
      - shared-net
    # No ports exposed — nginx handles external traffic

  postgres-cs:
    image: pgvector/pgvector:pg17
    container_name: course-supporter-db
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-course_supporter}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB:-course_supporter}
    volumes:
      - pgdata-cs:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-course_supporter}"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - default

networks:
  shared-net:
    external: true

volumes:
  pgdata-cs:
  netdata-config:
  netdata-lib:
```

### Monitoring (PD-015)

Три шари моніторингу:

**1. Netdata (системні метрики)**

```yaml
# додати до docker-compose.prod.yaml
  netdata:
    image: netdata/netdata:stable
    container_name: netdata
    restart: unless-stopped
    cap_add:
      - SYS_PTRACE
    security_opt:
      - apparmor:unconfined
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - netdata-config:/etc/netdata
      - netdata-lib:/var/lib/netdata
    networks:
      - default
      - shared-net
    # No ports — nginx proxies
```

Dashboard доступний через nginx на `api.pythoncourse.me/netdata/` (з basic auth).
~100-150 MB RAM. Метрики: CPU, RAM, disk I/O, network, Docker containers.

**2. Netdata Alerts → Telegram**

Вбудовані alerts з коробки + custom thresholds:
- Disk space < 20% → warning, < 10% → critical
- RAM usage > 85% → warning
- CPU sustained > 90% → warning
- Container restart → critical

Telegram notification через Netdata agent — потрібен bot token + chat ID.

**3. UptimeRobot (зовнішній)**

Free tier: пінг `https://api.pythoncourse.me/health` кожні 5 хв.
Alert в Telegram якщо API недоступний ззовні (мережа, DNS, nginx, app).
Налаштовується за 2 хвилини, без задачі в спрінті.

### Streaming Upload (PD-013)

Проблема: FastAPI `UploadFile` за замовчуванням зберігає файл у RAM (SpooledTemporaryFile, поріг 1MB).
Для 1GB відео — потрібен chunk-by-chunk streaming напряму в S3.

```
Client ──1GB──→ nginx (proxy_request_buffering off)
                  → FastAPI (async read chunks)
                    → S3 multipart upload (part = 10MB)
```

Зміни:

1. **`S3Client.upload_stream()`** — новий метод з `create_multipart_upload` / `upload_part` / `complete_multipart_upload`. Кожен part = 10MB, ніколи не тримаємо більше одного part в RAM.

2. **Upload endpoint** — замінити `file.read()` на async chunk reader:
```python
async def _stream_to_s3(file: UploadFile, s3: S3Client, key: str) -> int:
    """Stream upload file to S3 via multipart, return total bytes."""
    total = 0
    async for chunk in s3.multipart_upload(key):
        data = await file.read(10 * 1024 * 1024)  # 10MB chunks
        if not data:
            break
        await chunk.send(data)
        total += len(data)
    return total
```

3. **nginx** — `proxy_request_buffering off` щоб nginx стрімив напряму в upstream без збереження на диск.

Результат: upload 1GB відео використовує ~10-20 MB RAM незалежно від розміру файлу.

### GitHub Actions Deploy

```yaml
# .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  test:
    # existing lint + typecheck + test job
    ...

  deploy:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4

      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd /opt/course-supporter
            git pull origin main
            docker compose -f docker-compose.prod.yaml build
            docker compose -f docker-compose.prod.yaml up -d
            docker compose -f docker-compose.prod.yaml exec app \
              alembic upgrade head
            echo "Deploy complete: $(date)"
```

---

## Definition of Done

- [ ] `api.pythoncourse.me/health` відповідає з DB/S3 status
- [ ] API key auth працює (401 без ключа, 403 з wrong scope)
- [ ] Rate limiting працює (429 при перевищенні)
- [ ] Tenant A не бачить дані Tenant B
- [ ] Push to main → auto-deploy
- [ ] `llm_calls` містить `tenant_id` для білінгу
- [ ] Swagger UI доступний на `api.pythoncourse.me/docs`
- [ ] Netdata dashboard з alerts → Telegram
- [ ] UptimeRobot пінгує `/health` → Telegram
- [ ] Smoke test script проходить

---

## Ризики

| Ризик | Мітигація |
| :---- | :---- |
| RAM не вистачить | Моніторинг htop після deploy, 2 uvicorn workers (не 4) |
| nginx routing conflict | Окремий server block по subdomain, тестуємо локально |
| certbot для нового subdomain | DNS A-record до deploy, certbot --webroot |
| Rate limiter memory leak | TTL на старих записах, max dict size |
| Upload 1GB timeout | nginx timeouts 900s, `proxy_request_buffering off`, streaming |
| Disk space під час upload | `proxy_request_buffering off` мінімізує temp files, моніторинг df |
| Netdata RAM overhead | ~100-150 MB, моніторити після deploy, можна обмежити `mem_limit` |

---

## Що НЕ входить

- Frontend / admin UI (CLI достатньо)
- Redis для rate limiting (in-memory для single instance)
- Docker registry (build on VPS, не push image)
- Prometheus / Grafana (Netdata достатньо на цьому етапі)
- Backup automation (ручний pg_dump поки)
- Load balancing (один інстанс)
