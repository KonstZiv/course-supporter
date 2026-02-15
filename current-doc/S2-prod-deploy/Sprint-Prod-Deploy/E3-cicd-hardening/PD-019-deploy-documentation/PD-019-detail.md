# PD-019: Deploy Documentation — Detail

## Контекст

Після завершення всіх технічних задач — фіксуємо процедури в документації. Документ повинен бути self-sufficient для першого deploy та повторних деплоїв.

## Структура документа

```
docs/
└── deployment.md
```

## Зміст `docs/deployment.md`

### 1. Prerequisites

- VPS з Ubuntu 22/24, Docker, Docker Compose
- Домен з DNS A-record на VPS
- Backblaze B2 account з bucket + application key
- LLM API keys (Gemini, Anthropic, OpenAI, DeepSeek)
- GitHub repo access + SSH key для deploy

### 2. First Deploy (from scratch)

Покрокова інструкція:

```
1. Clone repo на VPS
2. Створити .env.prod з .env.prod.example
3. Створити shared-net (якщо не існує)
4. Запустити PostgreSQL
5. Build app image
6. Start app
7. Run migrations
8. Створити першого tenant + API key
9. DNS: додати A-record для api.pythoncourse.me
10. Nginx: додати server block
11. SSL: certbot для нового subdomain
12. Verify: curl health endpoint
13. Netdata: запустити, налаштувати alerts
14. UptimeRobot: додати monitor
```

Кожен крок — конкретна команда, не абстракція.

### 3. Subsequent Deploys

```bash
# Автоматично через GitHub Actions (push to main)
# Або вручну:
cd /opt/course-supporter
git pull origin main
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec -T app alembic upgrade head
curl -sf https://api.pythoncourse.me/health
```

### 4. Environment Variables Reference

Таблиця всіх env vars:

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | Yes | — | `production` or `development` |
| `POSTGRES_HOST` | Yes | — | PostgreSQL hostname |
| `POSTGRES_PASSWORD` | Yes | — | PostgreSQL password |
| `S3_ENDPOINT` | Yes | — | Backblaze B2 S3 endpoint |
| `S3_ACCESS_KEY` | Yes | — | B2 application key ID |
| `S3_SECRET_KEY` | Yes | — | B2 application key |
| `S3_BUCKET` | Yes | — | B2 bucket name |
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `CORS_ALLOWED_ORIGINS` | No | `[]` | JSON list of allowed origins |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| ... | ... | ... | ... |

### 5. Tenant Management

```bash
# Створити tenant:
docker compose exec app python -m scripts.manage_tenant create-tenant --name "Company A"

# Видати ключ:
docker compose exec app python -m scripts.manage_tenant create-key \
    --tenant "Company A" --scopes prep,check --label production

# Список:
docker compose exec app python -m scripts.manage_tenant list-tenants
docker compose exec app python -m scripts.manage_tenant list-keys --tenant "Company A"

# Відкликати:
docker compose exec app python -m scripts.manage_tenant revoke-key --prefix cs_live_abc1
```

### 6. Monitoring

- Netdata: `https://api.pythoncourse.me/netdata/` (basic auth)
- UptimeRobot: external ping every 5 min
- Logs: `docker compose logs -f app`
- DB: `docker compose exec postgres-cs psql -U course_supporter`

### 7. Backup & Restore

```bash
# Backup:
docker compose exec postgres-cs pg_dump -U course_supporter course_supporter > backup_$(date +%Y%m%d).sql

# Restore:
cat backup.sql | docker compose exec -T postgres-cs psql -U course_supporter course_supporter
```

### 8. Rollback

```bash
# Відкатити app:
git log --oneline -5
git checkout <prev-commit>
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app

# Відкатити міграцію:
docker compose exec app alembic downgrade -1
```

### 9. Troubleshooting

| Проблема | Діагностика | Рішення |
|---|---|---|
| 502 Bad Gateway | `docker compose ps` — app running? | `docker compose up -d app` |
| DB connection refused | `docker compose logs postgres-cs` | Перевірити POSTGRES_HOST |
| S3 upload fail | Health check S3 status | Перевірити B2 credentials |
| SSL certificate expired | `certbot certificates` | `certbot renew` |
| OOM kill | `docker inspect app` OOMKilled | Зменшити workers, перевірити streaming |

### 10. Architecture Diagram

Включити діаграму з Sprint-Prod-Deploy.md.

## Definition of Done

- [ ] `docs/deployment.md` створено
- [ ] First deploy instructions повні та робочі
- [ ] Env vars reference актуальний
- [ ] Troubleshooting top-5
- [ ] Rollback procedure описана
- [ ] Документ оновлений відповідно до фінальної реалізації
