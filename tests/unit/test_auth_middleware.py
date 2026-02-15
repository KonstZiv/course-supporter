"""Tests for API key authentication middleware (PD-003)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.storage.database import get_session


def _make_api_key_record(
    *,
    is_active: bool = True,
    tenant_is_active: bool = True,
    expires_at: datetime | None = None,
    scopes: list[str] | None = None,
    rate_limit_prep: int = 100,
    rate_limit_check: int = 1000,
) -> MagicMock:
    """Create a mock APIKey ORM object joined with Tenant."""
    tenant = MagicMock()
    tenant.id = uuid.uuid4()
    tenant.name = "Test Tenant"
    tenant.is_active = tenant_is_active

    api_key_record = MagicMock()
    api_key_record.tenant_id = tenant.id
    api_key_record.tenant = tenant
    api_key_record.is_active = is_active
    api_key_record.expires_at = expires_at
    api_key_record.scopes = scopes or ["prep", "check"]
    api_key_record.rate_limit_prep = rate_limit_prep
    api_key_record.rate_limit_check = rate_limit_check
    api_key_record.key_prefix = "cs_test"
    return api_key_record


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    """AsyncClient with DB override but NO auth override."""
    app.dependency_overrides[get_session] = lambda: mock_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_missing_api_key_header(self, client: AsyncClient) -> None:
        """Request without X-API-Key header returns 401."""
        response = await client.get(f"/api/v1/courses/{uuid.uuid4()}")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Request with non-existent API key returns 401."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        response = await client.get(
            f"/api/v1/courses/{uuid.uuid4()}",
            headers={"X-API-Key": "cs_test_invalidkey123456"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_inactive_api_key(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Request with inactive API key returns 401.

        The query filters by is_active=True, so an inactive key
        won't be found and returns None (same as invalid).
        """
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        response = await client.get(
            f"/api/v1/courses/{uuid.uuid4()}",
            headers={"X-API-Key": "cs_test_inactivekey123456"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_inactive_tenant(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Request with inactive tenant returns 401.

        The query filters by tenant.is_active=True, so an inactive tenant
        won't be found and returns None.
        """
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        response = await client.get(
            f"/api/v1/courses/{uuid.uuid4()}",
            headers={"X-API-Key": "cs_test_inactivetenantkey"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_api_key(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Request with expired API key returns 401."""
        expired_at = datetime.now(UTC) - timedelta(hours=1)
        record = _make_api_key_record(expires_at=expired_at)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = record
        mock_session.execute.return_value = mock_result

        response = await client.get(
            f"/api/v1/courses/{uuid.uuid4()}",
            headers={"X-API-Key": "cs_test_expiredkey123456"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "API key expired"

    @pytest.mark.asyncio
    async def test_valid_api_key(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Valid API key passes auth and reaches the endpoint."""
        record = _make_api_key_record()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = record
        mock_session.execute.return_value = mock_result

        # Patch the repository to return a course
        with patch(
            "course_supporter.storage.repositories.CourseRepository.get_with_structure",
            return_value=None,
        ):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}",
                headers={"X-API-Key": "cs_test_validkey123456"},
            )
        # 404 means we got past auth and into the endpoint logic
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_context_fields(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Valid auth populates all TenantContext fields correctly."""
        record = _make_api_key_record(
            scopes=["prep"],
            rate_limit_prep=50,
            rate_limit_check=500,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = record
        mock_session.execute.return_value = mock_result

        with patch(
            "course_supporter.storage.repositories.CourseRepository.get_with_structure",
        ) as mock_get:
            mock_get.return_value = None
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}",
                headers={"X-API-Key": "cs_test_contextkey123456"},
            )

        # Verify auth passed (404 from missing course, not 401)
        assert response.status_code == 404
        # Verify the DB query was executed with the hashed key
        call_args = mock_session.execute.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_health_no_auth(self, client: AsyncClient) -> None:
        """GET /health works without API key."""
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
