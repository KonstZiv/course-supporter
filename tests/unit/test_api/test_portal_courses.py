"""Unit tests for the portal course-listing route (Phase 6 T4a c1, KD17).

``get_current_student`` is overridden to a fixed StudentContext; the
enrollment JOIN is mocked at the repository. Visibility (publish-gate A) and
the active-only / oldest-first ordering are exercised at the repository level
by the live acceptance + integration; here we pin the route contract: bare
id+title, empty list for no enrollments, no extra fields.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_session
from course_supporter.auth.context import StudentContext
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
STUB_STUDENT = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)

_COURSES_URL = "/api/v1/portal/courses"


def _mock_course(title: str) -> MagicMock:
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = title
    return course


@pytest.fixture()
def mock_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestPortalCourseListing:
    async def test_lists_enrolled_courses_bare(self, client: AsyncClient) -> None:
        """Enrolled courses → list of {id, title} in repository order."""
        courses = [_mock_course("Python 101"), _mock_course("Algorithms")]
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=courses,
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body == [
            {"id": str(courses[0].id), "title": "Python 101"},
            {"id": str(courses[1].id), "title": "Algorithms"},
        ]

    async def test_empty_when_no_enrollments(self, client: AsyncClient) -> None:
        """No enrollments → empty list, never an error (nothing to leak)."""
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=[],
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_no_extra_fields_leaked(self, client: AsyncClient) -> None:
        """The card is bare — no counts, no tree, no tenant on the wire."""
        course = _mock_course("Solo")
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=[course],
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        assert set(resp.json()[0].keys()) == {"id", "title"}
