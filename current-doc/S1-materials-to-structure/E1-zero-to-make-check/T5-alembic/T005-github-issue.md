# S1-005: Alembic та початкова міграція

## Мета

Створити повну схему БД для Sprint 1 через Alembic міграцію: 8 таблиць, pgvector extension, cascading deletes. Після `alembic upgrade head` — база готова для API.

## Що робимо

1. **SQLAlchemy ORM-моделі** (`storage/orm.py`): `Course`, `SourceMaterial`, `SlideVideoMapping`, `Module`, `Lesson`, `Concept` (з Vector(1536) для майбутнього RAG), `Exercise`, `LLMCall`. UUIDv7 (time-ordered) для всіх PK через `uuid-utils`.
2. **Database engine** (`storage/database.py`): async engine + session factory з psycopg v3, FastAPI-dependency для DI
3. **Alembic init** з sync template (psycopg v3 підтримує sync нативно), `env.py` бере URL з `config.py` (не hardcoded)
4. **Початкова міграція**: autogenerate + ручна правка (`CREATE EXTENSION IF NOT EXISTS vector`, перевірка enum types)
5. **Makefile**: команди `db-upgrade`, `db-downgrade`, `db-reset`, `migrate msg="..."`

Ключові рішення: UUIDv7 генерується Python-side (не DB), JSONB для гнучких полів (examples, slide_references, web_references), LLMCall без FK до Course (незалежне логування).

## Очікуваний результат

```bash
docker compose up -d         # БД піднята (S1-003)
uv run alembic upgrade head  # 8 таблиць створено
```

## Контрольні точки

- [ ] `uv run alembic upgrade head` — без помилок
- [ ] `\dt` у psql — 8 таблиць + alembic_version
- [ ] `\d concepts` — колонка `embedding` типу `vector(1536)`
- [ ] `SELECT extname FROM pg_extension WHERE extname = 'vector'` — pgvector активний
- [ ] `uv run alembic downgrade base && uv run alembic upgrade head` — ідемпотентний цикл
- [ ] Видалення Course каскадно видаляє modules → lessons → concepts/exercises
- [ ] `uv run pytest tests/unit/test_orm_models.py` — зелений

## Залежності

- **Блокується:** S1-003 (PostgreSQL), S1-004 (config з database_url)
- **Блокує:** S1-018 (SourceMaterial CRUD), S1-022 (збереження структури курсу), весь Epic 5 (API)

## Деталі

Повний spec (ORM-моделі, env.py, тести, пояснення рішень): **S1-005-alembic.md**
