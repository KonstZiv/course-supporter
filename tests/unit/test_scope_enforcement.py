"""Tests for service scope enforcement (PD-004)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import CourseRepository


def _make_tenant(scopes: list[str]) -> TenantContext:
    """Create a TenantContext with given scopes."""
    return TenantContext(
        tenant_id=uuid.uuid4(),
        tenant_name="test-tenant",
        scopes=scopes,
        rate_limit_prep=100,
        rate_limit_check=1000,
        key_prefix="cs_test",
    )


def _make_course_mock() -> MagicMock:
    """Create a mock Course ORM object."""
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = "Test"
    course.description = None
    course.created_at = datetime.now(UTC)
    course.updated_at = datetime.now(UTC)
    return course


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestScopeEnforcement:
    @pytest.mark.asyncio
    async def test_prep_scope_allows_prep_endpoint(
        self, mock_session: AsyncMock
    ) -> None:
        """Tenant with 'prep' scope can access POST /courses."""
        tenant = _make_tenant(scopes=["prep"])
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    CourseRepository, "create", return_value=_make_course_mock()
                ):
                    response = await client.post(
                        "/api/v1/courses",
                        json={"title": "Test Course"},
                    )
            assert response.status_code == 201
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_check_scope_denied_prep_endpoint(
        self, mock_session: AsyncMock
    ) -> None:
        """Tenant with only 'check' scope is denied POST /courses."""
        tenant = _make_tenant(scopes=["check"])
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/courses",
                    json={"title": "Test Course"},
                )
            assert response.status_code == 403
            assert "Requires scope: prep" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_prep_scope_denied_check_only_endpoint(self) -> None:
        """Tenant with 'prep' scope is denied a 'check'-only endpoint.

        Since no 'check'-only endpoint exists yet, test via a temporary
        FastAPI app with a check-only route.
        """
        test_app = FastAPI()
        check_dep = Depends(require_scope("check"))

        @test_app.get("/check-only")
        async def check_only(
            tenant: TenantContext = check_dep,
        ) -> dict[str, str]:
            return {"ok": "true"}

        tenant = _make_tenant(scopes=["prep"])
        test_app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url="http://test",
            ) as client:
                response = await client.get("/check-only")
            assert response.status_code == 403
            assert "Requires scope: check" in response.json()["detail"]
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_shared_endpoint_allows_prep(self, mock_session: AsyncMock) -> None:
        """Tenant with 'prep' scope can access shared GET /courses/{id}."""
        tenant = _make_tenant(scopes=["prep"])
        course = _make_course_mock()
        course.modules = []
        course.source_materials = []
        course.learning_goal = None
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    CourseRepository, "get_with_structure", return_value=course
                ):
                    response = await client.get(f"/api/v1/courses/{course.id}")
            assert response.status_code == 200
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_shared_endpoint_allows_check(self, mock_session: AsyncMock) -> None:
        """Tenant with 'check' scope can access shared GET /courses/{id}."""
        tenant = _make_tenant(scopes=["check"])
        course = _make_course_mock()
        course.modules = []
        course.source_materials = []
        course.learning_goal = None
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    CourseRepository, "get_with_structure", return_value=course
                ):
                    response = await client.get(f"/api/v1/courses/{course.id}")
            assert response.status_code == 200
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_both_scopes_tenant(self, mock_session: AsyncMock) -> None:
        """Tenant with both scopes can access all endpoints."""
        tenant = _make_tenant(scopes=["prep", "check"])
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                # prep endpoint
                with patch.object(
                    CourseRepository, "create", return_value=_make_course_mock()
                ):
                    resp_prep = await client.post(
                        "/api/v1/courses",
                        json={"title": "Test"},
                    )
                assert resp_prep.status_code == 201

                # shared endpoint
                course = _make_course_mock()
                course.modules = []
                course.source_materials = []
                course.learning_goal = None
                with patch.object(
                    CourseRepository, "get_with_structure", return_value=course
                ):
                    resp_shared = await client.get(f"/api/v1/courses/{course.id}")
                assert resp_shared.status_code == 200
        finally:
            app.dependency_overrides.clear()
