"""Tests for GET /courses/{id} with nested structure."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import CourseRepository


def _make_concept_mock(*, title: str = "Variables") -> MagicMock:
    """Create a mock Concept ORM object."""
    concept = MagicMock()
    concept.id = uuid.uuid4()
    concept.title = title
    concept.definition = "A named storage location"
    concept.examples = ["x = 1"]
    concept.timecodes = ["00:05:00"]
    concept.slide_references = [1, 2]
    concept.web_references = [{"url": "https://example.com", "title": "Ref"}]
    return concept


def _make_exercise_mock(*, description: str = "Write a loop") -> MagicMock:
    """Create a mock Exercise ORM object."""
    exercise = MagicMock()
    exercise.id = uuid.uuid4()
    exercise.description = description
    exercise.reference_solution = "for i in range(10): print(i)"
    exercise.grading_criteria = "Correct output"
    exercise.difficulty_level = 3
    return exercise


def _make_lesson_mock(
    *,
    title: str = "Intro",
    concepts: list[MagicMock] | None = None,
    exercises: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock Lesson ORM object."""
    lesson = MagicMock()
    lesson.id = uuid.uuid4()
    lesson.title = title
    lesson.order = 0
    lesson.video_start_timecode = "00:00:00"
    lesson.video_end_timecode = "00:10:00"
    lesson.slide_range = {"start": 1, "end": 5}
    lesson.concepts = concepts or []
    lesson.exercises = exercises or []
    return lesson


def _make_module_mock(
    *, title: str = "Basics", lessons: list[MagicMock] | None = None
) -> MagicMock:
    """Create a mock Module ORM object."""
    module = MagicMock()
    module.id = uuid.uuid4()
    module.title = title
    module.description = "Fundamentals"
    module.learning_goal = "Learn basics"
    module.difficulty = "easy"
    module.order = 0
    module.lessons = lessons or []
    return module


def _make_source_material_mock(
    *, source_type: str = "video", status: str = "done"
) -> MagicMock:
    """Create a mock SourceMaterial ORM object."""
    sm = MagicMock()
    sm.id = uuid.uuid4()
    sm.source_type = source_type
    sm.source_url = "https://example.com/video.mp4"
    sm.filename = "video.mp4"
    sm.status = status
    sm.created_at = datetime.now(UTC)
    return sm


def _make_course_mock(
    *,
    modules: list[MagicMock] | None = None,
    source_materials: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock Course ORM object with nested structure."""
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = "Python 101"
    course.description = "Intro to Python"
    course.learning_goal = "Learn Python basics"
    course.created_at = datetime.now(UTC)
    course.updated_at = datetime.now(UTC)
    course.modules = modules or []
    course.source_materials = source_materials or []
    return course


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestGetCourseDetailAPI:
    @pytest.mark.asyncio
    async def test_get_course_returns_200(self, client: AsyncClient) -> None:
        """GET /api/v1/courses/{id} returns 200 for existing course."""
        course = _make_course_mock()
        with patch.object(CourseRepository, "get_with_structure", return_value=course):
            response = await client.get(f"/api/v1/courses/{course.id}")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_course_not_found_returns_404(self, client: AsyncClient) -> None:
        """GET /api/v1/courses/{id} returns 404 for missing course."""
        with patch.object(CourseRepository, "get_with_structure", return_value=None):
            response = await client.get(f"/api/v1/courses/{uuid.uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_course_includes_basic_fields(self, client: AsyncClient) -> None:
        """Response includes id, title, description, learning_goal."""
        course = _make_course_mock()
        with patch.object(CourseRepository, "get_with_structure", return_value=course):
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert data["id"] == str(course.id)
        assert data["title"] == "Python 101"
        assert data["description"] == "Intro to Python"
        assert data["learning_goal"] == "Learn Python basics"

    @pytest.mark.asyncio
    async def test_get_course_empty_structure(self, client: AsyncClient) -> None:
        """Course with no modules or materials returns empty lists."""
        course = _make_course_mock()
        with patch.object(CourseRepository, "get_with_structure", return_value=course):
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert data["modules"] == []
        assert data["source_materials"] == []

    @pytest.mark.asyncio
    async def test_get_course_includes_source_materials(
        self, client: AsyncClient
    ) -> None:
        """Response includes source materials."""
        sm = _make_source_material_mock()
        course = _make_course_mock(source_materials=[sm])
        with patch.object(CourseRepository, "get_with_structure", return_value=course):
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert len(data["source_materials"]) == 1
        assert data["source_materials"][0]["source_type"] == "video"
        assert data["source_materials"][0]["status"] == "done"

    @pytest.mark.asyncio
    async def test_get_course_nested_modules_lessons_concepts(
        self, client: AsyncClient
    ) -> None:
        """Response includes full nested structure."""
        concept = _make_concept_mock()
        exercise = _make_exercise_mock()
        lesson = _make_lesson_mock(concepts=[concept], exercises=[exercise])
        module = _make_module_mock(lessons=[lesson])
        course = _make_course_mock(modules=[module])
        with patch.object(CourseRepository, "get_with_structure", return_value=course):
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert len(data["modules"]) == 1
        mod = data["modules"][0]
        assert mod["title"] == "Basics"
        assert mod["difficulty"] == "easy"
        assert len(mod["lessons"]) == 1
        les = mod["lessons"][0]
        assert les["title"] == "Intro"
        assert les["slide_range"] == {"start": 1, "end": 5}
        assert len(les["concepts"]) == 1
        assert les["concepts"][0]["title"] == "Variables"
        assert len(les["exercises"]) == 1
        assert les["exercises"][0]["description"] == "Write a loop"


class TestCourseRepositoryGetWithStructure:
    @pytest.mark.asyncio
    async def test_get_with_structure_executes_query(
        self, mock_session: AsyncMock
    ) -> None:
        """get_with_structure() executes select with options."""
        course = _make_course_mock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = course
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session)
        result = await repo.get_with_structure(course.id)
        assert result == course
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_with_structure_returns_none(
        self, mock_session: AsyncMock
    ) -> None:
        """get_with_structure() returns None for missing course."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session)
        result = await repo.get_with_structure(uuid.uuid4())
        assert result is None
