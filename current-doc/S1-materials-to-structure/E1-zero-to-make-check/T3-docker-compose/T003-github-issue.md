# S1-003: Docker Compose середовище

## Мета

Підняти локальну інфраструктуру однією командою: `docker compose up -d` → PostgreSQL 17 з pgvector + MinIO (S3-compatible storage). Додаток працює локально через `uv run`, не в контейнері.

## Що робимо

1. **docker-compose.yaml:** два сервіси (postgres, minio) + одноразовий init-контейнер для створення S3-бакету
2. **PostgreSQL 17 Alpine:** офіційний образ, pgvector extension через init-script (`scripts/init-db.sh`). Якщо alpine не містить pgvector — fallback на образ `pgvector/pgvector:pg17`
3. **MinIO:** S3-сумісний storage, API на :9000, Console UI на :9001. Бакет `course-materials` створюється автоматично
4. **Health checks:** для обох сервісів, щоб залежні контейнери чекали готовності
5. **Makefile:** команди `up`, `down`, `reset` (з видаленням volumes), `logs`, `ps`

Credentials беруться з `.env` — ті самі змінні `POSTGRES_USER`, `POSTGRES_PASSWORD`, `S3_ACCESS_KEY`, що і для додатку.

## Очікуваний результат

```bash
cp .env.example .env
docker compose up -d     # 30 сек — все піднялось
docker compose ps        # postgres: healthy, minio: healthy
```

PostgreSQL готовий для Alembic міграцій (S1-005), MinIO готовий для upload файлів курсу.

## Контрольні точки

- [ ] `docker compose up -d` — обидва сервіси стартують без помилок
- [ ] `docker compose ps` — статус `healthy` для postgres та minio
- [ ] PostgreSQL: `SELECT extname FROM pg_extension WHERE extname = 'vector'` → повертає рядок
- [ ] MinIO: `curl -s http://localhost:9000/minio/health/live` → HTTP 200
- [ ] MinIO Console: `http://localhost:9001` — видно бакет `course-materials`
- [ ] `docker compose down -v && docker compose up -d` — чистий рестарт працює (init-script повторно створює extension та бакет)

## Залежності

- **Блокується:** S1-001 (репозиторій, `.env.example`)
- **Блокує:** S1-005 (Alembic міграції потребують БД)

## Деталі

Повний spec (docker-compose.yaml, init-db.sh, перевірки, альтернативи): **S1-003-docker-compose.md**
