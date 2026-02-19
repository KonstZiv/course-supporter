# PD-009: docker-compose.prod.yaml — Detail

## Production Compose

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: course-supporter-app
    restart: unless-stopped
    env_file: .env.prod
    depends_on:
      postgres-cs:
        condition: service_healthy
    networks:
      - default
      - shared-net
    # No ports — nginx proxies via shared-net

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
```

## .env.prod.example

```bash
# === App ===
ENVIRONMENT=production
LOG_LEVEL=INFO
CORS_ALLOWED_ORIGINS=["https://pythoncourse.me"]

# === PostgreSQL ===
POSTGRES_USER=course_supporter
POSTGRES_PASSWORD=<generate-strong-password>
POSTGRES_DB=course_supporter
POSTGRES_HOST=postgres-cs
POSTGRES_PORT=5432

# === Backblaze B2 (S3-compatible) ===
S3_ENDPOINT=https://s3.us-west-004.backblazeb2.com
S3_ACCESS_KEY=<backblaze-app-key-id>
S3_SECRET_KEY=<backblaze-app-key>
S3_BUCKET=course-supporter-materials

# === LLM API Keys ===
GEMINI_API_KEY=<key>
ANTHROPIC_API_KEY=<key>
OPENAI_API_KEY=<key>
DEEPSEEK_API_KEY=<key>
```

## Важливі деталі

**POSTGRES_HOST=postgres-cs** — hostname = container_name в default мережі compose.

**shared-net: external: true** — мережа створена Django compose, наш compose тільки підключається.

**Порядок запуску**: PostgreSQL повинен бути healthy перед app. `depends_on.condition: service_healthy` гарантує це.

**Entrypoint**: Alembic migration НЕ в entrypoint — запускається окремо при deploy (`docker compose exec app alembic upgrade head`). Це дозволяє відкат міграцій окремо від app.

## Startup Commands

```bash
# First deploy:
docker compose -f docker-compose.prod.yaml up -d postgres-cs
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec app alembic upgrade head
docker compose -f docker-compose.prod.yaml exec app \
    python -m scripts.manage_tenant create-tenant --name "System"

# Subsequent deploys:
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec app alembic upgrade head
```

## Результати верифікації

```
docker compose -f docker-compose.prod.yaml config  → valid (з .env.prod)
make check                                          → 385 passed
```

## Definition of Done

- [x] `docker-compose.prod.yaml` працює
- [x] `.env.prod.example` з усіма змінними
- [x] App підключається до `shared-net`
- [x] PostgreSQL з healthcheck та persistent volume
- [x] `make check` зелений
- [x] Документ оновлений відповідно до фінальної реалізації
