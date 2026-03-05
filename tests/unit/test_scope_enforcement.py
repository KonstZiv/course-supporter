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
from course_supporter.storage.material_node_repository import MaterialNodeRepository


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


def _make_node_mock(tenant_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock MaterialNode ORM object."""
    node = MagicMock()
    node.id = uuid.uuid4()
    node.tenant_id = tenant_id or uuid.uuid4()
    node.parent_materialnode_id = None
    node.title = "Test"
    node.description = None
    node.learning_goal = None
    node.expected_knowledge = None
    node.expected_skills = None
    node.order = 0
    node.node_fingerprint = None
    node.created_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    return node


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
        """Tenant with 'prep' scope can access POST /nodes."""
        tenant = _make_tenant(scopes=["prep"])
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    MaterialNodeRepository,
                    "create",
                    return_value=_make_node_mock(tenant.tenant_id),
                ):
                    response = await client.post(
                        "/api/v1/nodes",
                        json={"title": "Test Node"},
                    )
            assert response.status_code == 201
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_check_scope_denied_prep_endpoint(
        self, mock_session: AsyncMock
    ) -> None:
        """Tenant with only 'check' scope is denied POST /nodes."""
        tenant = _make_tenant(scopes=["check"])
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/nodes",
                    json={"title": "Test Node"},
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
        """Tenant with 'prep' scope can access shared GET /nodes/{id}."""
        tenant = _make_tenant(scopes=["prep"])
        node = _make_node_mock(tenant.tenant_id)
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    MaterialNodeRepository, "get_by_id", return_value=node
                ):
                    response = await client.get(f"/api/v1/nodes/{node.id}")
            assert response.status_code == 200
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_shared_endpoint_allows_check(self, mock_session: AsyncMock) -> None:
        """Tenant with 'check' scope can access shared GET /nodes/{id}."""
        tenant = _make_tenant(scopes=["check"])
        node = _make_node_mock(tenant.tenant_id)
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with patch.object(
                    MaterialNodeRepository, "get_by_id", return_value=node
                ):
                    response = await client.get(f"/api/v1/nodes/{node.id}")
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
                # prep endpoint (POST /nodes)
                with patch.object(
                    MaterialNodeRepository,
                    "create",
                    return_value=_make_node_mock(tenant.tenant_id),
                ):
                    resp_prep = await client.post(
                        "/api/v1/nodes",
                        json={"title": "Test"},
                    )
                assert resp_prep.status_code == 201

                # shared endpoint (GET /nodes/{id})
                node = _make_node_mock(tenant.tenant_id)
                with patch.object(
                    MaterialNodeRepository, "get_by_id", return_value=node
                ):
                    resp_shared = await client.get(f"/api/v1/nodes/{node.id}")
                assert resp_shared.status_code == 200
        finally:
            app.dependency_overrides.clear()
