"""Tests for POST /courses and CourseRepository."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import CourseRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _make_course_mock(
    *,
    title: str = "Python 101",
    description: str | None = "Intro",
) -> MagicMock:
    """Create a mock Course ORM object with all required fields."""
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = title
    course.description = description
    course.created_at = datetime.now(UTC)
    course.updated_at = datetime.now(UTC)
    return course


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


class TestCreateCourseAPI:
    @pytest.mark.asyncio
    async def test_create_course_returns_201(self, client: AsyncClient) -> None:
        """POST /api/v1/courses returns 201 on success."""
        with patch.object(CourseRepository, "create", return_value=_make_course_mock()):
            response = await client.post(
                "/api/v1/courses",
                json={"title": "Python 101"},
            )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_create_course_returns_id(self, client: AsyncClient) -> None:
        """POST /api/v1/courses returns course with UUID id."""
        course = _make_course_mock()
        with patch.object(CourseRepository, "create", return_value=course):
            response = await client.post(
                "/api/v1/courses",
                json={"title": "Python 101"},
            )
        data = response.json()
        assert data["id"] == str(course.id)

    @pytest.mark.asyncio
    async def test_create_course_with_description(self, client: AsyncClient) -> None:
        """POST /api/v1/courses accepts and returns description."""
        course = _make_course_mock(description="Intro to Python")
        with patch.object(CourseRepository, "create", return_value=course):
            response = await client.post(
                "/api/v1/courses",
                json={
                    "title": "Python 101",
                    "description": "Intro to Python",
                },
            )
        assert response.status_code == 201
        assert response.json()["description"] == "Intro to Python"

    @pytest.mark.asyncio
    async def test_create_course_without_description(self, client: AsyncClient) -> None:
        """POST /api/v1/courses works without description."""
        course = _make_course_mock(description=None)
        with patch.object(CourseRepository, "create", return_value=course):
            response = await client.post(
                "/api/v1/courses",
                json={"title": "Python 101"},
            )
        assert response.status_code == 201
        assert response.json()["description"] is None

    @pytest.mark.asyncio
    async def test_create_course_empty_title_returns_422(
        self, client: AsyncClient
    ) -> None:
        """POST /api/v1/courses rejects empty title."""
        response = await client.post(
            "/api/v1/courses",
            json={"title": ""},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_course_missing_title_returns_422(
        self, client: AsyncClient
    ) -> None:
        """POST /api/v1/courses rejects missing title."""
        response = await client.post(
            "/api/v1/courses",
            json={},
        )
        assert response.status_code == 422


class TestCourseRepository:
    @pytest.mark.asyncio
    async def test_create_flushes_session(self, mock_session: AsyncMock) -> None:
        """create() calls flush, not commit."""
        repo = CourseRepository(mock_session, uuid.uuid4())
        await repo.create(title="Test")
        mock_session.flush.assert_awaited_once()
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_by_id_returns_course(self, mock_session: AsyncMock) -> None:
        """get_by_id() executes tenant-scoped select query."""
        course = _make_course_mock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = course
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session, uuid.uuid4())
        result = await repo.get_by_id(course.id)
        assert result == course

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none(self, mock_session: AsyncMock) -> None:
        """get_by_id() returns None for missing course."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session, uuid.uuid4())
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_all_returns_courses(self, mock_session: AsyncMock) -> None:
        """list_all() executes select query."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            _make_course_mock(),
            _make_course_mock(title="ML 201"),
        ]
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session, uuid.uuid4())
        courses = await repo.list_all()
        assert len(courses) == 2
