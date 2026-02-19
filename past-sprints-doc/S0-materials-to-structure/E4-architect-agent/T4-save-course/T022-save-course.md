# 📋 S1-022: Save Course Structure (CourseStructureRepository)

## Мета

Реалізувати `CourseStructureRepository` для збереження `CourseStructure` (Pydantic) в базу даних через ORM-моделі. Replace-стратегія: повторний виклик видаляє попередню структуру (cascade delete modules) і створює нову. Транзакційне збереження (all-or-nothing).

## Контекст

Четверта задача Epic 4. Залежить від S1-019 (CourseStructure Pydantic models). Не залежить напряму від S1-020/S1-021 (може розроблятись паралельно з ArchitectAgent).

ORM-моделі вже визначені в `storage/orm.py`:
- `Course` → `modules` (cascade="all, delete-orphan")
- `Module` → `lessons` (cascade="all, delete-orphan")
- `Lesson` → `concepts` (cascade="all, delete-orphan"), `exercises` (cascade="all, delete-orphan")

Cascade delete вже налаштований — видалення Module автоматично видаляє Lesson → Concept + Exercise.

Файл `storage/repositories.py` вже містить `SourceMaterialRepository` — додаємо `CourseStructureRepository` поруч.

---

## Acceptance Criteria

- [ ] `CourseStructureRepository.__init__` приймає `AsyncSession`
- [ ] `save(course_id, structure: CourseStructure) -> Course` — async method
- [ ] Delete existing modules (cascade) перед створенням нових
- [ ] Create ORM objects: Module → Lesson → Concept + Exercise з правильним mapping
- [ ] `order` field для Module і Lesson — auto-increment (enumerate)
- [ ] SlideRange → JSONB dict, WebReference → JSONB list of dicts
- [ ] `flush()` замість `commit()` — caller контролює transaction
- [ ] Course.title і Course.description оновлюються зі structure
- [ ] Static helpers: `_create_module`, `_create_lesson`, `_create_concept`, `_create_exercise`
- [ ] ~12 unit-тестів з mocked session, всі зелені
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/storage/repositories.py (додати після SourceMaterialRepository)

```python
# --- Add imports at top ---
from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
)
from course_supporter.storage.orm import (
    Concept,
    Course,
    Exercise,
    Lesson,
    Module,
    SourceMaterial,  # already imported
)


class CourseStructureRepository:
    """Repository for saving course structure to database.

    Implements replace strategy: existing modules (and their children
    via cascade delete) are removed before creating new ones from
    the Pydantic CourseStructure.

    Uses flush() instead of commit() — caller controls transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        course_id: uuid.UUID,
        structure: CourseStructure,
    ) -> Course:
        """Save course structure, replacing existing modules.

        Args:
            course_id: UUID of the parent course.
            structure: Pydantic CourseStructure from ArchitectAgent.

        Returns:
            Updated Course ORM instance with new modules.

        Raises:
            ValueError: If course not found.
        """
        course = await self._session.get(Course, course_id)
        if course is None:
            raise ValueError(f"Course not found: {course_id}")

        # Update course metadata from structure
        course.title = structure.title
        if structure.description:
            course.description = structure.description

        # Replace strategy: clear existing modules (cascade deletes children)
        course.modules.clear()
        await self._session.flush()

        # Create new modules from Pydantic structure
        for module_idx, module_data in enumerate(structure.modules):
            module = self._create_module(course_id, module_idx, module_data)
            self._session.add(module)

        await self._session.flush()
        return course

    @staticmethod
    def _create_module(
        course_id: uuid.UUID,
        order: int,
        data: ModuleOutput,
    ) -> Module:
        """Create Module ORM from Pydantic ModuleOutput."""
        module = Module(
            course_id=course_id,
            title=data.title,
            order=order,
        )
        for lesson_idx, lesson_data in enumerate(data.lessons):
            lesson = CourseStructureRepository._create_lesson(lesson_idx, lesson_data)
            module.lessons.append(lesson)
        return module

    @staticmethod
    def _create_lesson(order: int, data: LessonOutput) -> Lesson:
        """Create Lesson ORM from Pydantic LessonOutput."""
        lesson = Lesson(
            title=data.title,
            order=order,
            video_start_timecode=data.video_start_timecode,
            video_end_timecode=data.video_end_timecode,
            slide_range=(
                data.slide_range.model_dump() if data.slide_range else None
            ),
        )
        for concept_data in data.concepts:
            concept = CourseStructureRepository._create_concept(concept_data)
            lesson.concepts.append(concept)
        for exercise_data in data.exercises:
            exercise = CourseStructureRepository._create_exercise(exercise_data)
            lesson.exercises.append(exercise)
        return lesson

    @staticmethod
    def _create_concept(data: ConceptOutput) -> Concept:
        """Create Concept ORM from Pydantic ConceptOutput."""
        return Concept(
            title=data.title,
            definition=data.definition,
            examples=data.examples if data.examples else None,
            timecodes=data.timecodes if data.timecodes else None,
            slide_references=(
                data.slide_references if data.slide_references else None
            ),
            web_references=(
                [ref.model_dump() for ref in data.web_references]
                if data.web_references
                else None
            ),
        )

    @staticmethod
    def _create_exercise(data: ExerciseOutput) -> Exercise:
        """Create Exercise ORM from Pydantic ExerciseOutput."""
        return Exercise(
            description=data.description,
            reference_solution=data.reference_solution,
            grading_criteria=data.grading_criteria,
            difficulty_level=data.difficulty_level,
        )
```

---

## Тести

### tests/unit/test_course_structure_repository.py

```python
"""Tests for CourseStructureRepository."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
    SlideRange,
    WebReference,
)
from course_supporter.storage.repositories import CourseStructureRepository


def _make_course_mock(course_id: uuid.UUID) -> MagicMock:
    """Create a mock Course ORM object."""
    course = MagicMock()
    course.id = course_id
    course.title = "Old Title"
    course.description = "Old desc"
    course.modules = []
    return course


@pytest.fixture()
def course_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def mock_session(course_id: uuid.UUID) -> AsyncMock:
    """AsyncSession mock that returns a Course on get()."""
    session = AsyncMock()
    course = _make_course_mock(course_id)
    session.get.return_value = course
    return session


@pytest.fixture()
def minimal_structure() -> CourseStructure:
    """CourseStructure with one module, one lesson, one concept."""
    return CourseStructure(
        title="Python 101",
        description="Intro to Python",
        modules=[
            ModuleOutput(
                title="Basics",
                lessons=[
                    LessonOutput(
                        title="Variables",
                        concepts=[
                            ConceptOutput(
                                title="Assignment",
                                definition="Binding names to values",
                            )
                        ],
                    )
                ],
            )
        ],
    )


class TestCourseStructureRepositorySave:
    @pytest.mark.asyncio
    async def test_save_updates_course_title(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() updates course title from structure."""
        repo = CourseStructureRepository(mock_session)
        course = await repo.save(course_id, minimal_structure)
        assert course.title == "Python 101"

    @pytest.mark.asyncio
    async def test_save_updates_course_description(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() updates course description from structure."""
        repo = CourseStructureRepository(mock_session)
        course = await repo.save(course_id, minimal_structure)
        assert course.description == "Intro to Python"

    @pytest.mark.asyncio
    async def test_save_clears_existing_modules(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() clears existing modules before creating new ones."""
        course_mock = mock_session.get.return_value
        course_mock.modules = MagicMock()
        repo = CourseStructureRepository(mock_session)
        await repo.save(course_id, minimal_structure)
        course_mock.modules.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_creates_module_with_order(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() creates Module ORM with correct order index."""
        repo = CourseStructureRepository(mock_session)
        await repo.save(course_id, minimal_structure)
        # session.add should be called with Module objects
        assert mock_session.add.called

    @pytest.mark.asyncio
    async def test_save_raises_on_missing_course(
        self,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() raises ValueError if course not found."""
        session = AsyncMock()
        session.get.return_value = None
        repo = CourseStructureRepository(session)
        with pytest.raises(ValueError, match="Course not found"):
            await repo.save(course_id, minimal_structure)

    @pytest.mark.asyncio
    async def test_save_calls_flush(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() calls flush() (not commit)."""
        repo = CourseStructureRepository(mock_session)
        await repo.save(course_id, minimal_structure)
        assert mock_session.flush.call_count >= 1
        mock_session.commit.assert_not_called()


class TestCreateHelpers:
    def test_create_concept_with_web_references(self) -> None:
        """_create_concept maps WebReference to JSONB dicts."""
        data = ConceptOutput(
            title="OOP",
            definition="Object-oriented programming",
            web_references=[
                WebReference(
                    url="https://example.com",
                    title="Example",
                    description="A reference",
                )
            ],
        )
        concept = CourseStructureRepository._create_concept(data)
        assert concept.web_references is not None
        assert len(concept.web_references) == 1
        assert concept.web_references[0]["url"] == "https://example.com"

    def test_create_lesson_with_slide_range(self) -> None:
        """_create_lesson maps SlideRange to JSONB dict."""
        data = LessonOutput(
            title="Lesson 1",
            slide_range=SlideRange(start=1, end=10),
        )
        lesson = CourseStructureRepository._create_lesson(0, data)
        assert lesson.slide_range == {"start": 1, "end": 10}

    def test_create_lesson_without_slide_range(self) -> None:
        """_create_lesson handles None slide_range."""
        data = LessonOutput(title="Lesson 1")
        lesson = CourseStructureRepository._create_lesson(0, data)
        assert lesson.slide_range is None

    def test_create_exercise_maps_fields(self) -> None:
        """_create_exercise maps all fields correctly."""
        data = ExerciseOutput(
            description="Write a function",
            reference_solution="def f(): pass",
            grading_criteria="Works correctly",
            difficulty_level=4,
        )
        exercise = CourseStructureRepository._create_exercise(data)
        assert exercise.description == "Write a function"
        assert exercise.reference_solution == "def f(): pass"
        assert exercise.grading_criteria == "Works correctly"
        assert exercise.difficulty_level == 4

    def test_create_concept_empty_lists_become_none(self) -> None:
        """_create_concept converts empty lists to None for JSONB."""
        data = ConceptOutput(title="C1", definition="D1")
        concept = CourseStructureRepository._create_concept(data)
        assert concept.examples is None
        assert concept.timecodes is None
        assert concept.slide_references is None
        assert concept.web_references is None

    def test_create_module_with_multiple_lessons(self) -> None:
        """_create_module creates Module with ordered lessons."""
        data = ModuleOutput(
            title="Module 1",
            lessons=[
                LessonOutput(title="L1"),
                LessonOutput(title="L2"),
                LessonOutput(title="L3"),
            ],
        )
        module = CourseStructureRepository._create_module(uuid.uuid4(), 0, data)
        assert module.title == "Module 1"
        assert module.order == 0
        assert len(module.lessons) == 3
```

---

## Структура файлів

```
src/course_supporter/storage/
└── repositories.py              # UPDATE: add CourseStructureRepository

tests/unit/
└── test_course_structure_repository.py  # NEW: ~12 tests
```

---

## Кроки виконання

1. Додати imports до `storage/repositories.py`
2. Додати `CourseStructureRepository` клас з `save()` та static helpers
3. Створити `tests/unit/test_course_structure_repository.py`
4. `make check`

---

## Примітки

- **Replace strategy**: `course.modules.clear()` + `flush()` видаляє всі modules через cascade. Потім створюються нові. Це простіше ніж diff/merge і достатньо для MVP.
- **flush() не commit()**: caller (API endpoint або orchestrator) контролює transaction boundary. При помилці — rollback відміняє все.
- **Empty lists → None**: ORM JSONB nullable — зберігаємо `None` замість `[]` для consistency (DB level). Pydantic default — empty list.
- **order field**: `enumerate()` — 0-based. Module.order і Lesson.order задаються при створенні.
- **SlideRange serialization**: `data.slide_range.model_dump()` → `{"start": N, "end": M}` → зберігається в JSONB.
- **WebReference serialization**: `[ref.model_dump() for ref in data.web_references]` → list of dicts → зберігається в JSONB.
- **Cascade delete**: ORM relationships вже налаштовані з `cascade="all, delete-orphan"`. Clear modules → lessons, concepts, exercises видаляються автоматично.
