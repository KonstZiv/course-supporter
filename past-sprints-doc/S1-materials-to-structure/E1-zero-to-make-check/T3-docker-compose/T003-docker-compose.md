# 📋 S1-003: Docker Compose середовище

## Мета

Налаштувати локальне середовище розробки через Docker Compose: PostgreSQL з pgvector та MinIO (S3-compatible storage). Після виконання — `docker compose up -d` піднімає всю інфраструктуру, готову для роботи з додатком.

## Контекст

Залежить від S1-001 (репозиторій, `.env.example`). Додаток (FastAPI) запускається локально через `uv run`, НЕ в контейнері — Docker Compose тільки для інфраструктурних сервісів. Це спрощує debug та hot-reload під час розробки.

---

## Acceptance Criteria

- [x] `docker compose up -d` піднімає PostgreSQL та MinIO без помилок
- [x] PostgreSQL доступний на `localhost:5432` із розширенням pgvector
- [x] MinIO доступний на `localhost:9000` (API) та `localhost:9001` (Console)
- [x] Бакет `course-materials` створюється автоматично при старті
- [x] Credentials беруться з `.env` файлу (ті ж змінні, що в `.env.example`)
- [x] `docker compose down -v` повністю очищає дані (для чистого рестарту)
- [x] Health checks працюють для обох сервісів

---

## docker-compose.yaml

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "${POSTGRES_PORT:-5432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init-db.sh:/docker-entrypoint-initdb.d/init-db.sh:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 5

  minio:
    image: minio/minio:latest
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${S3_ACCESS_KEY}
      MINIO_ROOT_PASSWORD: ${S3_SECRET_KEY}
    ports:
      - "9000:9000"   # API
      - "9001:9001"   # Console UI
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 5

  minio-init:
    image: minio/mc:latest
    depends_on:
      minio:
        condition: service_healthy
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 ${S3_ACCESS_KEY} ${S3_SECRET_KEY};
      mc mb local/${S3_BUCKET} --ignore-existing;
      exit 0;
      "

volumes:
  postgres_data:
  minio_data:
```

### Пояснення архітектурних рішень

**pgvector/pgvector:pg17** — образ PostgreSQL 17 з pgvector, що гарантує наявність розширення. Обрано замість `postgres:17-alpine`, оскільки alpine-образ не містить pgvector з коробки. Змінні `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` передаються явно через `environment:` (без `env_file`, щоб не завантажувати зайві змінні в контейнер).

**pgvector** — активується через init script (`init-db.sh`) з `CREATE EXTENSION IF NOT EXISTS vector`. Бінарник вже присутній у образі `pgvector/pgvector:pg17`.

**MinIO** — S3-сумісний storage для файлів курсів (відео, PDF, тощо). Для MVP достатньо локальної файлової системи, але MinIO дозволяє:
- Тестувати S3-сумісний код без реального AWS
- Однакова поведінка dev/staging
- Console UI на порті 9001 для перегляду файлів

**minio-init** — одноразовий контейнер, який створює бакет `course-materials` і завершується. `depends_on` з `condition: service_healthy` гарантує, що MinIO вже готовий.

**Health checks** — обидва сервіси мають health checks. Це дозволяє Docker Compose (і в майбутньому CI) чекати готовності перед запуском залежних сервісів.

---

## scripts/init-db.sh

```bash
#!/bin/bash
set -e

# Встановлюємо pgvector extension
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

echo "pgvector extension created successfully"
```

> **Важливо:** файл має бути executable (`chmod +x scripts/init-db.sh`). Docker entrypoint виконує всі `.sh` та `.sql` файли з `/docker-entrypoint-initdb.d/` при першому створенні бази.

> **Примітка:** Використовуємо `pgvector/pgvector:pg17` напряму, без окремого Dockerfile. Кастомний Dockerfile потрібний лише якщо треба додаткові розширення.

---

## Оновлення .env.example

`.env.example` з S1-001 вже містить усі необхідні змінні. Для Docker Compose критичні:

```env
# Ці змінні використовуються і Docker Compose, і додатком
POSTGRES_USER=course_supporter
POSTGRES_PASSWORD=secret
POSTGRES_DB=course_supporter
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=course-materials
```

---

## Оновлення Makefile

Додати команди для Docker Compose:

```makefile
# --- Infrastructure ---

up:  ## Запустити інфраструктуру (PostgreSQL + MinIO)
	docker compose up -d
	@echo "Waiting for services..."
	@docker compose exec postgres pg_isready -U $${POSTGRES_USER:-course_supporter} > /dev/null 2>&1 && \
		echo "PostgreSQL: ready" || echo "PostgreSQL: waiting..."
	@echo "MinIO Console: http://localhost:9001"

down:  ## Зупинити інфраструктуру
	docker compose down

reset:  ## Зупинити та видалити всі дані (чистий рестарт)
	docker compose down -v

logs:  ## Показати логи сервісів
	docker compose logs -f

ps:  ## Статус сервісів
	docker compose ps
```

---

## Перевірка працездатності

### PostgreSQL + pgvector

```bash
# Підключитись до БД
docker compose exec postgres psql -U course_supporter -d course_supporter

# В psql:
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
# Очікуваний результат:
#  extname | extversion
# ---------+------------
#  vector  | 0.8.1
```

### MinIO

```bash
# Перевірити через curl
curl -s http://localhost:9000/minio/health/live
# Очікуваний результат: HTTP 200

# Або відкрити Console UI в браузері
# http://localhost:9001 (login: minioadmin / minioadmin)
# Має бути видно бакет course-materials
```

---

## .gitignore доповнення

Додати до `.gitignore` з S1-001:

```gitignore
# Docker volumes (якщо хтось використає bind mount замість named volume)
postgres_data/
minio_data/
```

---

## Кроки виконання

1. Створити `docker-compose.yaml`
2. Створити `scripts/init-db.sh`, зробити executable
3. Додати Docker-команди до Makefile
4. `cp .env.example .env` (якщо ще не зроблено)
5. `docker compose up -d`
6. Перевірити PostgreSQL: підключитись, перевірити pgvector extension
7. Перевірити MinIO: health endpoint + Console UI + наявність бакету
8. `docker compose down -v` + `docker compose up -d` — перевірити чистий рестарт
9. Commit + push

---

## Примітки

- **Додаток НЕ в Docker.** FastAPI запускається локально через `uv run uvicorn ...` для зручного debug/hot-reload. Контейнеризація додатку — задача Sprint 3+ (Dockerfile для staging/prod).
- **Named volumes** (`postgres_data`, `minio_data`) зберігають дані між `docker compose down` та `docker compose up`. Для повного очищення — `docker compose down -v`.
- **Порти** — PostgreSQL на 5432, MinIO API на 9000, MinIO Console на 9001. Якщо конфлікт з локальними сервісами — змінити в `.env` (`POSTGRES_PORT`).
- Якщо в команді хтось використовує Podman замість Docker — `docker-compose.yaml` сумісний з `podman-compose`.
