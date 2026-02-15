"""Tests for GET /courses/{id}/lessons/{lesson_id} and LessonRepository."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import LessonRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _make_concept_mock(*, title: str = "Variables") -> MagicMock:
    """Create a mock Concept ORM object."""
    concept = MagicMock()
    concept.id = uuid.uuid4()
    concept.title = title
    concept.definition = "A named storage location"
    concept.examples = ["x = 1"]
    concept.timecodes = None
    concept.slide_references = None
    concept.web_references = None
    return concept


def _make_exercise_mock(*, description: str = "Write a loop") -> MagicMock:
    """Create a mock Exercise ORM object."""
    exercise = MagicMock()
    exercise.id = uuid.uuid4()
    exercise.description = description
    exercise.reference_solution = None
    exercise.grading_criteria = None
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


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestGetLessonDetailAPI:
    @pytest.mark.asyncio
    async def test_get_lesson_returns_200(self, client: AsyncClient) -> None:
        """GET /api/v1/courses/{id}/lessons/{id} returns 200."""
        lesson = _make_lesson_mock()
        with patch.object(
            LessonRepository, "get_by_id_for_course", return_value=lesson
        ):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/lessons/{lesson.id}"
            )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_lesson_not_found_returns_404(self, client: AsyncClient) -> None:
        """GET lesson returns 404 when not found."""
        with patch.object(LessonRepository, "get_by_id_for_course", return_value=None):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/lessons/{uuid.uuid4()}"
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_lesson_wrong_course_returns_404(
        self, client: AsyncClient
    ) -> None:
        """GET lesson returns 404 when lesson belongs to different course."""
        with patch.object(LessonRepository, "get_by_id_for_course", return_value=None):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/lessons/{uuid.uuid4()}"
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_lesson_includes_basic_fields(self, client: AsyncClient) -> None:
        """Response includes lesson fields."""
        lesson = _make_lesson_mock()
        with patch.object(
            LessonRepository, "get_by_id_for_course", return_value=lesson
        ):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/lessons/{lesson.id}"
            )
        data = response.json()
        assert data["id"] == str(lesson.id)
        assert data["title"] == "Intro"
        assert data["video_start_timecode"] == "00:00:00"
        assert data["slide_range"] == {"start": 1, "end": 5}

    @pytest.mark.asyncio
    async def test_get_lesson_includes_concepts_and_exercises(
        self, client: AsyncClient
    ) -> None:
        """Response includes nested concepts and exercises."""
        concept = _make_concept_mock()
        exercise = _make_exercise_mock()
        lesson = _make_lesson_mock(concepts=[concept], exercises=[exercise])
        with patch.object(
            LessonRepository, "get_by_id_for_course", return_value=lesson
        ):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/lessons/{lesson.id}"
            )
        data = response.json()
        assert len(data["concepts"]) == 1
        assert data["concepts"][0]["title"] == "Variables"
        assert len(data["exercises"]) == 1
        assert data["exercises"][0]["description"] == "Write a loop"


class TestLessonRepository:
    @pytest.mark.asyncio
    async def test_get_by_id_for_course_returns_lesson(
        self, mock_session: AsyncMock
    ) -> None:
        """get_by_id_for_course() returns lesson when found."""
        lesson = _make_lesson_mock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = lesson
        mock_session.execute.return_value = mock_result
        repo = LessonRepository(mock_session)
        result = await repo.get_by_id_for_course(lesson.id, uuid.uuid4())
        assert result == lesson
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_id_for_course_returns_none(
        self, mock_session: AsyncMock
    ) -> None:
        """get_by_id_for_course() returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result
        repo = LessonRepository(mock_session)
        result = await repo.get_by_id_for_course(uuid.uuid4(), uuid.uuid4())
        assert result is None
