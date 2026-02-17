# Deployment Guide

Production deployment for **Course Supporter API** on a shared VPS with Docker Compose.

**Domain:** `api.pythoncourse.me`
**Architecture:** FastAPI app + PostgreSQL (pgvector) + Backblaze B2 (S3) + Netdata monitoring, behind shared nginx reverse proxy.

---

## 1. Prerequisites

- **VPS**: Ubuntu 22.04/24.04 with Docker and Docker Compose v2 installed
- **Domain**: DNS A-record pointing to VPS IP (e.g. `api.pythoncourse.me`)
- **Backblaze B2**: account with bucket and application key (S3-compatible API)
- **LLM API keys**: at least one of Gemini, Anthropic, OpenAI, DeepSeek
- **GitHub access**: deploy SSH key (read-only) added to the repository
- **Nginx**: running on the VPS (shared with other services via `shared-net` Docker network)
- **Optional**: `jq` for JSON filtering in shell commands (`sudo apt install jq`)

---

## 2. First Deploy (from scratch)

### 2.1. Create deploy user

```bash
sudo useradd -m -s /bin/bash deploy-course-supporter
sudo usermod -aG docker deploy-course-supporter
```

### 2.2. Generate deploy SSH key

```bash
sudo -u deploy-course-supporter ssh-keygen -t ed25519 -f /home/deploy-course-supporter/.ssh/deploy_key -N ""
```

Add the public key to GitHub repository as a **read-only deploy key**.

### 2.3. Clone repository

```bash
sudo mkdir -p /opt/course-supporter
sudo chown deploy-course-supporter:deploy-course-supporter /opt/course-supporter

sudo -u deploy-course-supporter bash -c '
  GIT_SSH_COMMAND="ssh -i /home/deploy-course-supporter/.ssh/deploy_key" \
  git clone git@github.com:KonstZiv/course-supporter.git /opt/course-supporter
'
```

> All subsequent commands should be run as the `deploy-course-supporter` user:
> `sudo -i -u deploy-course-supporter`

### 2.4. Create Docker network

```bash
docker network inspect shared-net >/dev/null 2>&1 || docker network create shared-net
```

> This network is shared with the main nginx proxy. The command is a no-op if it already exists.

### 2.5. Configure environment

```bash
cd /opt/course-supporter
cp .env.prod.example .env.prod
# Edit .env.prod with actual credentials:
nano .env.prod
```

See [Environment Variables Reference](#4-environment-variables-reference) for all variables.

### 2.6. Build and start services

```bash
cd /opt/course-supporter
docker compose -f docker-compose.prod.yaml build
docker compose -f docker-compose.prod.yaml up -d
```

### 2.7. Run database migrations

```bash
docker compose -f docker-compose.prod.yaml exec -T app alembic upgrade head
```

### 2.8. Configure nginx

Add a server block for `api.pythoncourse.me`. Full reference: `deploy/nginx/course-supporter.conf`.

Key parts:

```nginx
# HTTP → HTTPS redirect
server {
    listen 80;
    server_name api.pythoncourse.me;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# HTTPS
server {
    listen 443 ssl;
    server_name api.pythoncourse.me;

    ssl_certificate /etc/letsencrypt/live/api.pythoncourse.me/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.pythoncourse.me/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 1G;
    # Stream uploads directly to upstream without disk buffering (video files up to 1GB)
    proxy_request_buffering off;

    # Docker DNS — resolve at request time, not at startup
    resolver 127.0.0.11 valid=30s;
    set $course_supporter http://course-supporter-app:8000;
    set $netdata_backend http://netdata:19999;

    location / {
        proxy_pass $course_supporter;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /netdata/ {
        auth_basic "Monitoring";
        auth_basic_user_file /etc/nginx/.htpasswd_netdata;
        proxy_pass $netdata_backend/;
        proxy_set_header Host $host;
    }
}
```

> See `deploy/nginx/course-supporter.conf` for TLS hardening, security headers, and upload timeouts.

Reload nginx:

```bash
# Nginx runs as a separate container (not part of docker-compose.prod.yaml)
docker exec nginx nginx -s reload
```

### 2.9. SSL certificate

```bash
certbot certonly --webroot -w /var/www/html -d api.pythoncourse.me
```

Update nginx config with certificate paths, then reload:

```bash
# Nginx runs as a separate container (not part of docker-compose.prod.yaml)
docker exec nginx nginx -s reload
```

### 2.10. Create first tenant

```bash
docker compose -f docker-compose.prod.yaml exec app \
  python -m scripts.manage_tenant create-tenant --name "MyCompany"

docker compose -f docker-compose.prod.yaml exec app \
  python -m scripts.manage_tenant create-key \
    --tenant "MyCompany" --scopes prep,check --label production
```

> The API key is displayed **once** — save it immediately.

### 2.11. Verify deployment

```bash
# Health check:
curl -sf https://api.pythoncourse.me/health | jq .

# Auth check:
curl -H "X-API-Key: cs_live_..." https://api.pythoncourse.me/api/v1/courses
```

---

## 3. Subsequent Deploys

### Automated (GitHub Actions)

Trigger manually via **Actions > Deploy > Run workflow** in GitHub.

The workflow:
1. SSH into VPS
2. `git pull origin main`
3. Build app image
4. Restart app container
5. Run migrations
6. Health check with retries (10 attempts, 5s interval)

### Manual

```bash
cd /opt/course-supporter
git pull origin main
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec -T app alembic upgrade head
curl -sf https://api.pythoncourse.me/health | jq .
```

---

## 4. Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | Yes | (Must be set) | `production` for JSON logging and debug off |
| `LOG_LEVEL` | No | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CORS_ALLOWED_ORIGINS` | No | `[]` | JSON list of allowed origins, e.g. `["https://pythoncourse.me"]` |
| `CORS_ALLOW_CREDENTIALS` | No | `false` | Allow credentials in CORS |
| `CORS_ALLOWED_METHODS` | No | `["GET","POST"]` | Allowed HTTP methods |
| `CORS_ALLOWED_HEADERS` | No | `["Content-Type","X-API-Key"]` | Allowed request headers |
| `POSTGRES_USER` | No | `course_supporter` | PostgreSQL user |
| `POSTGRES_PASSWORD` | Yes | — | PostgreSQL password |
| `POSTGRES_DB` | No | `course_supporter` | PostgreSQL database name |
| `POSTGRES_HOST` | Yes | — | PostgreSQL host (`postgres-cs` in Docker) |
| `POSTGRES_PORT` | No | `5432` | PostgreSQL port |
| `S3_ENDPOINT` | Yes | — | S3-compatible endpoint URL |
| `S3_ACCESS_KEY` | Yes | — | S3 access key ID |
| `S3_SECRET_KEY` | Yes | — | S3 secret key |
| `S3_BUCKET` | Yes | `course-materials` | S3 bucket name |
| `GEMINI_API_KEY` | No | — | Google Gemini API key |
| `ANTHROPIC_API_KEY` | No | — | Anthropic API key |
| `OPENAI_API_KEY` | No | — | OpenAI API key |
| `DEEPSEEK_API_KEY` | No | — | DeepSeek API key |

> At least one LLM API key is required for the ArchitectAgent to function.

---

## 5. Tenant Management

All commands run inside the app container:

```bash
# Prefix for all commands:
docker compose -f docker-compose.prod.yaml exec app python -m scripts.manage_tenant
```

| Command | Options | Description |
|---|---|---|
| `create-tenant` | `--name "Company"` | Create a new tenant |
| `create-key` | `--tenant "Company" --scopes prep,check --label prod` | Generate API key (shown once) |
| `list-tenants` | — | List all tenants with status and key counts |
| `list-keys` | `--tenant "Company"` | List keys for a tenant |
| `revoke-key` | `--prefix cs_live_abc1` | Revoke a specific API key |
| `deactivate-tenant` | `--name "Company"` | Deactivate tenant (all keys become invalid) |

Optional rate limit flags for `create-key`:
- `--rate-prep 60` — requests per minute for prep scope
- `--rate-check 300` — requests per minute for check scope

---

## 6. Monitoring

### Health check

```bash
curl -sf https://api.pythoncourse.me/health | jq .
# Returns: {"status": "ok", "checks": {"db": "ok", "s3": "ok"}, "timestamp": "..."}
# HTTP 200 = healthy, HTTP 503 = degraded
```

### Netdata dashboard

- URL: `https://api.pythoncourse.me/netdata/`
- Protected by HTTP basic auth (`.htpasswd_netdata`)
- Monitors: CPU, RAM, disk, Docker containers, network I/O

Setup Telegram alerts — see `current-doc/S2-prod-deploy/infrastructure/netdata-setup.md`.

### Application logs

```bash
# Follow logs:
docker compose -f docker-compose.prod.yaml logs -f app

# Filter errors (JSON format in production):
docker compose -f docker-compose.prod.yaml logs app --no-log-prefix | jq 'select(.level == "error")'

# Last 50 lines:
docker compose -f docker-compose.prod.yaml logs app --tail=50
```

### External monitoring

Configure UptimeRobot (or similar) to ping `https://api.pythoncourse.me/health` every 5 minutes.

---

## 7. Backup & Restore

### Database backup

```bash
docker compose -f docker-compose.prod.yaml exec postgres-cs \
  pg_dump -U course_supporter course_supporter > backup_$(date +%Y%m%d).sql
```

### Database restore

```bash
cat backup.sql | docker compose -f docker-compose.prod.yaml exec -T postgres-cs \
  psql -U course_supporter course_supporter
```

> S3 data (Backblaze B2) is managed externally and has its own versioning/lifecycle policies.

---

## 8. Rollback

### Application rollback

```bash
cd /opt/course-supporter
git log --oneline -5                    # find target commit
git checkout <commit-hash>
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
curl -sf https://api.pythoncourse.me/health | jq .
```

### Migration rollback

```bash
docker compose -f docker-compose.prod.yaml exec -T app alembic downgrade -1
```

> Always verify the target migration before running downgrade in production.

---

## 9. Troubleshooting

| Problem | Diagnosis | Solution |
|---|---|---|
| **502 Bad Gateway** | `docker compose -f docker-compose.prod.yaml ps` — is app running? | `docker compose -f docker-compose.prod.yaml up -d app` |
| **DB connection refused** | `docker compose -f docker-compose.prod.yaml logs postgres-cs` | Check `POSTGRES_HOST=postgres-cs` in `.env.prod` |
| **S3 upload failure** | Health check shows `"s3": "error: ..."` | Verify B2 credentials and endpoint in `.env.prod` |
| **SSL certificate expired** | `certbot certificates` | `certbot renew && docker exec nginx nginx -s reload` |
| **OOM kill** | `docker compose -f docker-compose.prod.yaml ps -q app \| xargs docker inspect \| jq '.[0].State.OOMKilled'` | Reduce workers, check memory-heavy operations |
| **Slow responses** | Check app logs for `latency_ms` values | Review LLM provider latency, check DB query times |
| **Rate limit hit (429)** | Response includes `Retry-After` header | Wait or adjust rate limits via `create-key` |

### Useful debug commands

```bash
# Container status:
docker compose -f docker-compose.prod.yaml ps

# Database shell:
docker compose -f docker-compose.prod.yaml exec postgres-cs \
  psql -U course_supporter course_supporter

# App shell:
docker compose -f docker-compose.prod.yaml exec app /bin/bash

# Check disk space:
df -h /var/lib/docker
```
