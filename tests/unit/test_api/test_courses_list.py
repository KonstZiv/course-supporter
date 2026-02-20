"""Tests for GET /api/v1/courses (list with pagination)."""

from __future__ import annotations

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
    scopes=["prep", "check"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _mock_course(
    *, title: str = "Python 101", description: str | None = "Intro"
) -> MagicMock:
    """Create a mock Course ORM object."""
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


class TestListCourses:
    """GET /api/v1/courses"""

    async def test_returns_200_with_items(self, client: AsyncClient) -> None:
        """Returns paginated course list."""
        courses = [_mock_course(title="A"), _mock_course(title="B")]
        with (
            patch.object(CourseRepository, "list_all", return_value=courses),
            patch.object(CourseRepository, "count", return_value=2),
        ):
            resp = await client.get("/api/v1/courses")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 2
        assert data["limit"] == 20
        assert data["offset"] == 0

    async def test_empty_list(self, client: AsyncClient) -> None:
        """Returns empty items with total=0."""
        with (
            patch.object(CourseRepository, "list_all", return_value=[]),
            patch.object(CourseRepository, "count", return_value=0),
        ):
            resp = await client.get("/api/v1/courses")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_custom_limit_and_offset(self, client: AsyncClient) -> None:
        """Respects limit and offset query parameters."""
        with (
            patch.object(CourseRepository, "list_all", return_value=[]) as mock_list,
            patch.object(CourseRepository, "count", return_value=50),
        ):
            resp = await client.get("/api/v1/courses?limit=10&offset=20")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 20
        assert data["total"] == 50
        mock_list.assert_awaited_once_with(limit=10, offset=20)

    async def test_limit_exceeds_max_returns_422(self, client: AsyncClient) -> None:
        """Limit > 100 is rejected by validation."""
        resp = await client.get("/api/v1/courses?limit=101")
        assert resp.status_code == 422

    async def test_negative_offset_returns_422(self, client: AsyncClient) -> None:
        """Negative offset is rejected by validation."""
        resp = await client.get("/api/v1/courses?offset=-1")
        assert resp.status_code == 422

    async def test_limit_zero_returns_422(self, client: AsyncClient) -> None:
        """Limit=0 is rejected (ge=1)."""
        resp = await client.get("/api/v1/courses?limit=0")
        assert resp.status_code == 422

    async def test_response_contains_course_fields(self, client: AsyncClient) -> None:
        """Each item contains expected course fields."""
        course = _mock_course(title="Advanced Python", description="Deep dive")
        with (
            patch.object(CourseRepository, "list_all", return_value=[course]),
            patch.object(CourseRepository, "count", return_value=1),
        ):
            resp = await client.get("/api/v1/courses")
        item = resp.json()["items"][0]
        assert item["id"] == str(course.id)
        assert item["title"] == "Advanced Python"
        assert item["description"] == "Deep dive"
        assert "created_at" in item
        assert "updated_at" in item
