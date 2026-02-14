"""Tests for CourseStructureRepository."""

import uuid
from unittest.mock import AsyncMock, MagicMock

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
    course.learning_goal = None
    course.expected_knowledge = None
    course.expected_skills = None
    course.modules = MagicMock()
    return course


@pytest.fixture()
def course_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def mock_session(course_id: uuid.UUID) -> AsyncMock:
    """AsyncSession mock that returns a Course on get()."""
    session = AsyncMock()
    # add() is sync in SQLAlchemy — use MagicMock to avoid coroutine warning
    session.add = MagicMock()
    course = _make_course_mock(course_id)
    session.get.return_value = course
    return session


@pytest.fixture()
def minimal_structure() -> CourseStructure:
    """CourseStructure with one module, one lesson, one concept."""
    return CourseStructure(
        title="Python 101",
        description="Intro to Python",
        learning_goal="Learn Python basics",
        expected_knowledge=["Python syntax"],
        expected_skills=["Write scripts"],
        modules=[
            ModuleOutput(
                title="Basics",
                description="Core features",
                learning_goal="Understand variables",
                expected_knowledge=["Variable types"],
                expected_skills=["Assign variables"],
                difficulty="easy",
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
    async def test_save_updates_learning_fields(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() updates learning_goal, expected_knowledge, expected_skills."""
        repo = CourseStructureRepository(mock_session)
        course = await repo.save(course_id, minimal_structure)
        assert course.learning_goal == "Learn Python basics"
        assert course.expected_knowledge == ["Python syntax"]
        assert course.expected_skills == ["Write scripts"]

    @pytest.mark.asyncio
    async def test_save_clears_existing_modules(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() clears existing modules before creating new ones."""
        repo = CourseStructureRepository(mock_session)
        await repo.save(course_id, minimal_structure)
        course_mock = mock_session.get.return_value
        course_mock.modules.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_creates_modules(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
        minimal_structure: CourseStructure,
    ) -> None:
        """save() creates Module ORM objects via session.add."""
        repo = CourseStructureRepository(mock_session)
        await repo.save(course_id, minimal_structure)
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
    async def test_save_calls_flush_not_commit(
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

    @pytest.mark.asyncio
    async def test_save_empty_learning_fields_become_none(
        self,
        mock_session: AsyncMock,
        course_id: uuid.UUID,
    ) -> None:
        """save() stores None for empty learning fields."""
        structure = CourseStructure(title="Test")
        repo = CourseStructureRepository(mock_session)
        course = await repo.save(course_id, structure)
        assert course.learning_goal is None
        assert course.expected_knowledge is None
        assert course.expected_skills is None


class TestCreateHelpers:
    def test_create_module_with_learning_fields(self) -> None:
        """_create_module maps learning-oriented fields."""
        data = ModuleOutput(
            title="OOP",
            description="Object-oriented programming",
            learning_goal="Understand OOP",
            expected_knowledge=["Classes", "Inheritance"],
            expected_skills=["Create classes"],
            difficulty="hard",
        )
        module = CourseStructureRepository._create_module(uuid.uuid4(), 0, data)
        assert module.title == "OOP"
        assert module.description == "Object-oriented programming"
        assert module.learning_goal == "Understand OOP"
        assert module.expected_knowledge == ["Classes", "Inheritance"]
        assert module.expected_skills == ["Create classes"]
        assert module.difficulty == "hard"
        assert module.order == 0

    def test_create_module_empty_learning_fields_become_none(self) -> None:
        """_create_module converts empty learning fields to None."""
        data = ModuleOutput(title="M1")
        module = CourseStructureRepository._create_module(uuid.uuid4(), 0, data)
        assert module.description is None
        assert module.learning_goal is None
        assert module.expected_knowledge is None
        assert module.expected_skills is None

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
