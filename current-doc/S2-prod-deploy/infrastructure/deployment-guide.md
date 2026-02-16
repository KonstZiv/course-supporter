# Deployment Guide

Step-by-step guide to deploy Course Supporter API on a fresh VPS or redeploy after changes.

## Prerequisites

- VPS with Ubuntu 22.04+ and Docker Engine installed
- Domain `api.pythoncourse.me` with DNS A/CNAME pointing to VPS IP
- Existing nginx running in a separate Docker compose (Django project) with `shared-net`
- SSH access to VPS

## 1. Clone Repository

```bash
ssh user@vps
cd /opt
git clone git@github.com:KonstZiv/course-supporter.git
cd course-supporter
```

## 2. Create shared-net (if not exists)

```bash
docker network create shared-net || true
```

## 3. Configure Environment

```bash
cp .env.example .env.prod
nano .env.prod
```

Required variables:

```bash
# PostgreSQL
POSTGRES_USER=course_supporter
POSTGRES_PASSWORD=<strong-password>
POSTGRES_DB=course_supporter
POSTGRES_HOST=postgres-cs
POSTGRES_PORT=5432

# Backblaze B2 (S3-compatible)
S3_ENDPOINT=https://s3.us-west-004.backblazeb2.com
S3_ACCESS_KEY=<b2-key-id>
S3_SECRET_KEY=<b2-application-key>
S3_BUCKET=course-materials

# LLM API Keys
GEMINI_API_KEY=<key>
ANTHROPIC_API_KEY=<key>
OPENAI_API_KEY=<key>
DEEPSEEK_API_KEY=<key>

# App
ENVIRONMENT=production
LOG_LEVEL=INFO
```

## 4. Build and Start Services

```bash
docker compose -f docker-compose.prod.yaml build
docker compose -f docker-compose.prod.yaml up -d
```

Verify containers:

```bash
docker compose -f docker-compose.prod.yaml ps
# Expected: app (running), postgres-cs (healthy), netdata (running)
```

## 5. Run Database Migrations

```bash
docker compose -f docker-compose.prod.yaml exec app alembic upgrade head
```

## 6. Configure Nginx

Copy nginx config to the Django project's nginx directory:

```bash
# Adjust path to your nginx config directory
cp deploy/nginx/course-supporter.conf /path/to/django-project/nginx/conf.d/

# Reload nginx
docker exec <nginx-container> nginx -s reload
```

## 7. SSL Certificate (first time only)

```bash
# Create certificate via certbot (webroot mode)
docker exec <nginx-container> certbot certonly \
    --webroot -w /var/www/html \
    -d api.pythoncourse.me \
    --agree-tos --email your@email.com

# Reload nginx to pick up new cert
docker exec <nginx-container> nginx -s reload
```

Certificate auto-renews via certbot's cron job.

## 8. Create First Tenant

```bash
docker compose -f docker-compose.prod.yaml exec app \
    python -m course_supporter.scripts.manage_tenant \
    create-tenant --name "MyCompany"

docker compose -f docker-compose.prod.yaml exec app \
    python -m course_supporter.scripts.manage_tenant \
    create-key --tenant-name "MyCompany" --label "production" --scopes prep,check
```

Save the displayed API key — it's shown only once.

## 9. Smoke Test

```bash
# Health check (no auth)
curl https://api.pythoncourse.me/health
# Expected: {"status": "ok", "checks": {"db": "ok", "s3": "ok"}, ...}

# Auth check
curl -H "X-API-Key: cs_live_..." https://api.pythoncourse.me/api/v1/courses
# Expected: 200 OK

# No auth → 401
curl https://api.pythoncourse.me/api/v1/courses
# Expected: 401 Unauthorized
```

## 10. Setup Netdata Monitoring

See [netdata-setup.md](netdata-setup.md) for Telegram alerts and custom thresholds.

---

## Redeployment (after code changes)

```bash
ssh user@vps
cd /opt/course-supporter
git pull origin main
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec app alembic upgrade head
```

## Troubleshooting

### App won't start

```bash
# Check logs
docker compose -f docker-compose.prod.yaml logs app --tail=50

# Check if postgres is healthy
docker compose -f docker-compose.prod.yaml ps postgres-cs
```

### 502 Bad Gateway from nginx

```bash
# Check if app container is running and on shared-net
docker inspect course-supporter-app --format '{{json .NetworkSettings.Networks}}' | jq

# Verify nginx can resolve the container
docker exec <nginx-container> ping -c1 course-supporter-app
```

### Database connection refused

```bash
# Check if postgres is accepting connections
docker compose -f docker-compose.prod.yaml exec postgres-cs pg_isready

# Check .env.prod POSTGRES_HOST matches container name (postgres-cs)
```

### S3/B2 upload fails

```bash
# Verify credentials in .env.prod
# Check app logs for S3 errors
docker compose -f docker-compose.prod.yaml logs app | grep s3
```
