# 📋 S1-018: SourceMaterial Persistence (Repository)

## Мета

Реалізувати `SourceMaterialRepository` — CRUD для ORM `SourceMaterial` з async SQLAlchemy. Включає status machine з валідацією переходів (pending → processing → done/error) та автоматичними side effects (`processed_at`, `error_message`).

## Контекст

Не залежить від S1-011 — може виконуватися паралельно. Використовує існуючу ORM з Epic 1 (`storage/orm.py`): `SourceMaterial` з полями `status`, `content_snapshot`, `processed_at`, `error_message`. Database session з `storage/database.py`.

---

## Acceptance Criteria

- [ ] `SourceMaterialRepository` з CRUD: create, get_by_id, get_by_course_id, update_status, delete
- [ ] `create()` → `SourceMaterial` з status="pending"
- [ ] `get_by_id()` → `SourceMaterial | None`
- [ ] `get_by_course_id()` → `list[SourceMaterial]`
- [ ] `update_status()` — валідація переходів (pending→processing, processing→done, processing→error)
- [ ] `update_status("done")` → автоматично sets `processed_at`
- [ ] `update_status("error")` → sets `error_message`
- [ ] Invalid transition (pending→done) → `ValueError`
- [ ] `delete()` → видалення з БД
- [ ] ~8 unit-тестів з мокнутим `AsyncSession`
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/storage/repositories.py

```python
"""CRUD repositories for database operations."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import SourceMaterial

# Valid status transitions: current_status → set of allowed next statuses
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing"},
    "processing": {"done", "error"},
    "done": set(),     # terminal state
    "error": set(),    # terminal state
}


class SourceMaterialRepository:
    """Repository for SourceMaterial CRUD operations.

    Encapsulates database access for source materials with
    status machine validation for processing lifecycle.

    Status machine:
        pending → processing → done
                              → error

    Invalid transitions raise ValueError.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID,
        source_type: str,
        source_url: str,
        filename: str | None = None,
    ) -> SourceMaterial:
        """Create a new source material with status 'pending'.

        Args:
            course_id: FK to the parent course.
            source_type: One of 'video', 'presentation', 'text', 'web'.
            source_url: URL or path to the source file.
            filename: Optional original filename.

        Returns:
            The newly created SourceMaterial ORM instance.
        """
        material = SourceMaterial(
            course_id=course_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            status="pending",
        )
        self._session.add(material)
        await self._session.flush()
        return material

    async def get_by_id(self, material_id: uuid.UUID) -> SourceMaterial | None:
        """Get source material by its primary key.

        Args:
            material_id: UUID of the source material.

        Returns:
            SourceMaterial if found, None otherwise.
        """
        return await self._session.get(SourceMaterial, material_id)

    async def get_by_course_id(
        self, course_id: uuid.UUID
    ) -> list[SourceMaterial]:
        """Get all source materials for a given course.

        Args:
            course_id: UUID of the parent course.

        Returns:
            List of SourceMaterial instances (may be empty).
        """
        stmt = (
            select(SourceMaterial)
            .where(SourceMaterial.course_id == course_id)
            .order_by(SourceMaterial.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        material_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        content_snapshot: str | None = None,
    ) -> SourceMaterial:
        """Update processing status with validation and side effects.

        Valid transitions:
            pending → processing
            processing → done (sets processed_at)
            processing → error (sets error_message)

        Args:
            material_id: UUID of the source material.
            status: New status value.
            error_message: Required when transitioning to 'error'.
            content_snapshot: Optional content snapshot to save.

        Returns:
            Updated SourceMaterial instance.

        Raises:
            ValueError: If material not found or transition is invalid.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")

        current_status = material.status
        allowed = VALID_TRANSITIONS.get(current_status, set())

        if status not in allowed:
            raise ValueError(
                f"Invalid status transition: '{current_status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )

        material.status = status

        if status == "done":
            material.processed_at = datetime.now(timezone.utc)

        if status == "error" and error_message:
            material.error_message = error_message

        if content_snapshot is not None:
            material.content_snapshot = content_snapshot

        await self._session.flush()
        return material

    async def delete(self, material_id: uuid.UUID) -> None:
        """Delete a source material by ID.

        Args:
            material_id: UUID of the source material to delete.

        Raises:
            ValueError: If material not found.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")
        await self._session.delete(material)
        await self._session.flush()
```

---

## Тести

### tests/unit/test_ingestion/test_repository.py

```python
"""Tests for SourceMaterialRepository."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.storage.repositories import (
    SourceMaterialRepository,
    VALID_TRANSITIONS,
)


def _make_material(
    status: str = "pending",
    material_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock SourceMaterial ORM object."""
    mat = MagicMock()
    mat.id = material_id or uuid.uuid4()
    mat.course_id = uuid.uuid4()
    mat.source_type = "video"
    mat.source_url = "file:///v.mp4"
    mat.filename = "v.mp4"
    mat.status = status
    mat.content_snapshot = None
    mat.processed_at = None
    mat.error_message = None
    mat.created_at = datetime.now(timezone.utc)
    return mat


class TestCreate:
    async def test_create_material(self) -> None:
        """Adds SourceMaterial to session with pending status."""
        session = AsyncMock()
        repo = SourceMaterialRepository(session)

        course_id = uuid.uuid4()
        with patch(
            "course_supporter.storage.repositories.SourceMaterial"
        ) as MockMaterial:
            mock_instance = MagicMock()
            MockMaterial.return_value = mock_instance

            result = await repo.create(
                course_id=course_id,
                source_type="video",
                source_url="file:///v.mp4",
                filename="v.mp4",
            )

        session.add.assert_called_once_with(mock_instance)
        session.flush.assert_awaited_once()
        assert result is mock_instance


class TestGetById:
    async def test_get_by_id_found(self) -> None:
        """Returns SourceMaterial when found."""
        mat = _make_material()
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_id(mat.id)

        assert result is mat

    async def test_get_by_id_not_found(self) -> None:
        """Returns None when not found."""
        session = AsyncMock()
        session.get.return_value = None

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_id(uuid.uuid4())

        assert result is None


class TestGetByCourseId:
    async def test_get_by_course_id(self) -> None:
        """Returns list of materials for a course."""
        mat1 = _make_material()
        mat2 = _make_material()

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mat1, mat2]

        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        session = AsyncMock()
        session.execute.return_value = mock_result

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_course_id(uuid.uuid4())

        assert len(result) == 2


class TestUpdateStatus:
    async def test_update_status_to_processing(self) -> None:
        """pending → processing OK."""
        mat = _make_material(status="pending")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(mat.id, "processing")

        assert result.status == "processing"
        session.flush.assert_awaited()

    async def test_update_status_to_done(self) -> None:
        """processing → done, sets processed_at."""
        mat = _make_material(status="processing")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(mat.id, "done")

        assert result.status == "done"
        assert result.processed_at is not None

    async def test_update_status_to_error(self) -> None:
        """processing → error, sets error_message."""
        mat = _make_material(status="processing")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(
            mat.id, "error", error_message="Something broke"
        )

        assert result.status == "error"
        assert result.error_message == "Something broke"

    async def test_update_status_invalid_transition(self) -> None:
        """pending → done raises ValueError."""
        mat = _make_material(status="pending")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="Invalid status transition"):
            await repo.update_status(mat.id, "done")
```

---

## Структура файлів

```
src/course_supporter/storage/
├── database.py              # existing: async_session, get_session()
├── orm.py                   # existing: SourceMaterial ORM
└── repositories.py          # SourceMaterialRepository

tests/unit/test_ingestion/
└── test_repository.py       # ~8 tests
```

---

## Кроки виконання

1. Реалізувати `SourceMaterialRepository` в `storage/repositories.py`
2. Створити `tests/unit/test_ingestion/test_repository.py`
3. `make check`

---

## Примітки

- **Паралельно з S1-011**: ця задача не залежить від SourceProcessor/schemas, бо працює тільки з ORM.
- **flush vs commit**: використовуємо `flush()` замість `commit()` — caller контролює transaction boundary. Це дозволяє batch operations та rollback.
- **Status machine**: `VALID_TRANSITIONS` dict — простий і розширюваний. Якщо потрібен складніший state machine — `transitions` lib, але для 4 станів overkill.
- **ORM types**: `SourceMaterial.status` — `Enum("pending", "processing", "done", "error")` у БД. SQLAlchemy перевіряє при write, але валідація в коді дає кращі помилки.
- **processed_at timezone**: `datetime.now(timezone.utc)` — explicit UTC для consistency з server_default `func.now()` в ORM.
- **Розширюваність**: інші repository (CourseRepository, LLMCallRepository) будуть додані за аналогічним pattern при потребі.
