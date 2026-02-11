# 📋 S1-005: Alembic та початкова міграція

## Мета

Налаштувати Alembic для управління схемою БД та створити початкову міграцію з усіма таблицями Sprint 1. Після виконання — `alembic upgrade head` створює повну схему БД, готову для роботи з API.

## Контекст

Залежить від S1-003 (PostgreSQL працює) та S1-004 (config з `database_url` для Alembic). Ця задача створює SQLAlchemy ORM-моделі та першу міграцію.

---

## Acceptance Criteria

- [x] `uv run alembic upgrade head` створює всі таблиці без помилок
- [x] `uv run alembic downgrade base` відкатує до порожньої БД
- [x] `uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head` — ідемпотентний цикл
- [x] Усі таблиці зі схеми даних створені: courses, source_materials, slide_video_mappings, modules, lessons, concepts, exercises (renamed from tasks), llm_calls
- [x] pgvector extension використовується для поля `embedding` в concepts
- [x] Foreign keys та cascading deletes працюють коректно
- [x] Alembic читає DATABASE_URL з конфігурації додатку (не hardcoded)

---

## SQLAlchemy ORM-моделі

### src/course_supporter/storage/database.py

```python
"""Database engine and session configuration."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_dev,  # SQL logging тільки в dev
    pool_size=5,
    max_overflow=10,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency for DB session."""
    async with async_session() as session:
        yield session
```

### src/course_supporter/storage/orm.py

```python
"""SQLAlchemy ORM models for all Sprint 1 entities."""

import uuid
from datetime import datetime
from typing import Any

import uuid_utils as uuid7_lib
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-ordered) for use as default PK value."""
    return uuid.UUID(bytes=uuid7_lib.uuid7().bytes)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# ──────────────────────────────────────────────
# Course & Source Materials
# ──────────────────────────────────────────────


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    source_materials: Mapped[list["SourceMaterial"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    slide_video_mappings: Mapped[list["SlideVideoMapping"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    modules: Mapped[list["Module"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class SourceMaterial(Base):
    __tablename__ = "source_materials"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "video", "presentation", "text", "web",
            name="source_type_enum",
        )
    )
    source_url: Mapped[str] = mapped_column(String(2000))
    filename: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        Enum(
            "pending", "processing", "done", "error",
            name="processing_status_enum",
        ),
        default="pending",
    )
    content_snapshot: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(
        back_populates="source_materials"
    )


class SlideVideoMapping(Base):
    __tablename__ = "slide_video_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    slide_number: Mapped[int] = mapped_column(Integer)
    video_timecode: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(
        back_populates="slide_video_mappings"
    )


# ──────────────────────────────────────────────
# Course Structure
# ──────────────────────────────────────────────


class Module(Base):
    __tablename__ = "modules"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="modules")
    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="module", cascade="all, delete-orphan"
    )


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    order: Mapped[int] = mapped_column(Integer)
    video_start_timecode: Mapped[str | None] = mapped_column(String(20))
    video_end_timecode: Mapped[str | None] = mapped_column(String(20))
    slide_range: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    module: Mapped["Module"] = relationship(back_populates="lessons")
    concepts: Mapped[list["Concept"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    exercises: Mapped[list["Exercise"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )


class Concept(Base):
    __tablename__ = "concepts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    definition: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[Any] | None] = mapped_column(JSONB)
    timecodes: Mapped[list[Any] | None] = mapped_column(JSONB)
    slide_references: Mapped[list[Any] | None] = mapped_column(JSONB)
    web_references: Mapped[list[Any] | None] = mapped_column(JSONB)
    # WARNING: Vector dimension is tied to a specific embedding model.
    # 1536 = OpenAI text-embedding-3-small. Changing the model later
    # requires an ALTER COLUMN migration. Consider making this configurable
    # or choosing a model before committing to a dimension.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    lesson: Mapped["Lesson"] = relationship(back_populates="concepts")


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE")
    )
    description: Mapped[str] = mapped_column(Text)
    reference_solution: Mapped[str | None] = mapped_column(Text)
    grading_criteria: Mapped[str | None] = mapped_column(Text)
    difficulty_level: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    lesson: Mapped["Lesson"] = relationship(back_populates="exercises")


# ──────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=_uuid7
    )
    task_type: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

### Пояснення рішень

**UUIDv7 як native UUID** — `Mapped[uuid.UUID]` з `sqlalchemy.Uuid` зберігає UUID нативно. `uuid-utils` генерує UUIDv7 (time-ordered) на стороні Python — це дозволяє знати ID до INSERT (корисно для batch operations) та забезпечує природне сортування за часом створення.

**JSONB для гнучких полів** — `examples`, `timecodes`, `slide_references`, `web_references`, `slide_range` зберігаються як JSONB з explicit типами (`list[Any]`, `dict[str, Any]`). Це дозволяє зберігати структуровані дані без додаткових join-таблиць, що оптимально для MVP.

**Vector(1536)** — розмірність для OpenAI text-embedding-3-small. Для Sprint 1 embeddings не обчислюються (це Sprint 2 / RAG), але колонка створюється одразу, щоб не робити міграцію пізніше.

**Cascading deletes** — видалення Course каскадно видаляє всі пов'язані записи. Спрощує очищення тестових даних.

**LLMCall** — окрема таблиця для кожного виклику LLM. Не прив'язана FK до Course — дозволяє логувати виклики з будь-якого контексту. ModelRouter (S1-009) буде записувати сюди автоматично.

---

## Alembic конфігурація

### Ініціалізація

```bash
uv run alembic init migrations
```

### alembic.ini

Головна зміна — видалити hardcoded `sqlalchemy.url`, бо він задається програмно:

```ini
[alembic]
script_location = migrations

# sqlalchemy.url — задається в env.py через config.py
```

### migrations/env.py

```python
"""Alembic environment configuration.

Uses psycopg v3 sync engine for migrations.
Database URL is loaded from application settings (config.py).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from course_supporter.config import settings
from course_supporter.storage.orm import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL programmatically from application settings.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate support: point at our ORM metadata.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL so that
    calls to context.execute() emit SQL to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates a sync engine via psycopg v3 and runs migrations
    within a transaction.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

### Генерація початкової міграції

```bash
uv run alembic revision --autogenerate -m "initial_schema"
```

Перевірити згенерований файл — autogenerate іноді пропускає pgvector extension та enum types. На початку `upgrade()` додати:

```python
def upgrade() -> None:
    # Ensure pgvector extension exists
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ... autogenerated tables ...
```

---

## Тести

### tests/unit/test_orm_models.py

```python
"""Tests for ORM model definitions (no DB required)."""

from course_supporter.storage.orm import (
    Base,
    Concept,
    Course,
    Exercise,
    Lesson,
    LLMCall,
    Module,
    SlideVideoMapping,
    SourceMaterial,
)


class TestORMModels:
    """Verify ORM models are correctly defined."""

    def test_all_tables_registered(self) -> None:
        """All expected tables are in Base metadata."""
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "courses",
            "source_materials",
            "slide_video_mappings",
            "modules",
            "lessons",
            "concepts",
            "exercises",
            "llm_calls",
        }
        assert expected.issubset(table_names)

    def test_course_table_columns(self) -> None:
        """Course table has expected columns."""
        columns = {c.name for c in Course.__table__.columns}
        assert "id" in columns
        assert "title" in columns
        assert "created_at" in columns
        assert "updated_at" in columns

    def test_source_material_fk(self) -> None:
        """SourceMaterial has FK to courses."""
        fks = {
            fk.target_fullname
            for fk in SourceMaterial.__table__.foreign_keys
        }
        assert "courses.id" in fks

    def test_cascade_chain(self) -> None:
        """Verify cascade chain: Course -> Module -> Lesson -> Concept/Exercise."""
        # Module -> Course
        assert any(
            fk.target_fullname == "courses.id"
            for fk in Module.__table__.foreign_keys
        )
        # Lesson -> Module
        assert any(
            fk.target_fullname == "modules.id"
            for fk in Lesson.__table__.foreign_keys
        )
        # Concept -> Lesson
        assert any(
            fk.target_fullname == "lessons.id"
            for fk in Concept.__table__.foreign_keys
        )
        # Exercise -> Lesson
        assert any(
            fk.target_fullname == "lessons.id"
            for fk in Exercise.__table__.foreign_keys
        )

    def test_concept_has_vector_column(self) -> None:
        """Concept has embedding column for future RAG."""
        columns = {c.name for c in Concept.__table__.columns}
        assert "embedding" in columns

    def test_llm_call_not_linked_to_course(self) -> None:
        """LLMCall is independent — no FK to courses."""
        fks = {
            fk.target_fullname
            for fk in LLMCall.__table__.foreign_keys
        }
        assert len(fks) == 0

    def test_slide_video_mapping_fk(self) -> None:
        """SlideVideoMapping has FK to courses."""
        fks = {
            fk.target_fullname
            for fk in SlideVideoMapping.__table__.foreign_keys
        }
        assert "courses.id" in fks

    def test_ondelete_cascade_on_foreign_keys(self) -> None:
        """All FK constraints use CASCADE ondelete."""
        models_with_fks = [
            SourceMaterial,
            SlideVideoMapping,
            Module,
            Lesson,
            Concept,
            Exercise,
        ]
        for model in models_with_fks:
            for fk in model.__table__.foreign_keys:
                assert fk.ondelete == "CASCADE", (
                    f"{model.__tablename__}.{fk.parent.name} "
                    f"missing CASCADE ondelete"
                )
```

---

## Makefile доповнення

```makefile
# --- Database ---

migrate:  ## Створити нову міграцію (autogenerate)
	uv run alembic revision --autogenerate -m "$(msg)"

db-upgrade:  ## Застосувати міграції
	uv run alembic upgrade head

db-downgrade:  ## Відкатити останню міграцію
	uv run alembic downgrade -1

db-reset:  ## Повний ресет: downgrade до base + upgrade до head
	uv run alembic downgrade base
	uv run alembic upgrade head
```

Використання: `make migrate msg="add_feedback_table"`

---

## Кроки виконання

1. Створити `src/course_supporter/storage/database.py` (engine, session)
2. Створити `src/course_supporter/storage/orm.py` (усі ORM-моделі)
3. `uv run alembic init migrations`
4. Оновити `alembic.ini` — видалити hardcoded URL
5. Оновити `migrations/env.py` — використати config та Base.metadata
6. `uv run alembic revision --autogenerate -m "initial_schema"`
7. Перевірити згенерований файл — додати `CREATE EXTENSION IF NOT EXISTS vector`
8. `uv run alembic upgrade head` — застосувати міграцію
9. Перевірити в psql: `\dt` — всі таблиці є, `\d concepts` — embedding column type vector(1536)
10. `uv run alembic downgrade base && uv run alembic upgrade head` — ідемпотентність
11. Створити `tests/unit/test_orm_models.py`, запустити
12. Додати DB-команди в Makefile
13. Commit + push

---

## Примітки

- **Sync Alembic** — використовуємо sync template (default). psycopg v3 підтримує sync і async нативно — `postgresql+psycopg://` працює з `engine_from_config` (sync) і `create_async_engine` (async) без зміни URL.
- **Autogenerate** — зручно, але завжди переглядати результат. Типові проблеми: пропущені enum types, неправильний порядок CREATE/DROP, відсутність `CREATE EXTENSION`.
- **Vector(1536)** — розмірність embeddings задається при створенні колонки. Якщо пізніше зміниться модель (наприклад, на 3072-dimensional) — потрібна міграція з ALTER COLUMN.
- **Без Submission/Feedback** — ці таблиці з'являться в Sprint 2 (Guide Agent). Окрема міграція.
- **UUIDv7** — time-ordered UUID замість uuid4. Забезпечує природне сортування за часом та кращу продуктивність B-tree індексів. Генерується через `uuid-utils` (основна залежність).
- **Exercise замість Task** — перейменовано, щоб уникнути конфлікту з `asyncio.Task` та іншими Python builtins.
