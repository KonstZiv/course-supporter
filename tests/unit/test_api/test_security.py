"""Security hardening tests: CORS, error handling, debug mode."""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = lambda *a, **kw: None
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncGenerator[AsyncClient]:
    """AsyncClient that skips real DB and ModelRouter."""
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


class TestCORSRestriction:
    @pytest.mark.asyncio
    async def test_cors_production_restricted(self, client: AsyncClient) -> None:
        """Empty CORS origins (default) → preflight rejected."""
        response = await client.options(
            "/health",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in response.headers


class TestErrorNoStacktrace:
    @pytest.mark.asyncio
    async def test_error_no_stacktrace(self) -> None:
        """Unhandled exception returns generic message without stack trace."""
        from course_supporter.api.app import unhandled_exception_handler

        mock_request = Request(
            scope={"type": "http", "method": "GET", "path": "/test", "headers": []}
        )
        response = await unhandled_exception_handler(
            mock_request, RuntimeError("sensitive db error")
        )
        assert response.status_code == 500
        body = response.body.decode()
        assert "Internal server error" in body
        assert "sensitive" not in body
        assert "Traceback" not in body


class TestDebugMode:
    def test_debug_false_in_production(self) -> None:
        """Production environment → app.debug is False."""
        from course_supporter.config import Environment, Settings

        prod_settings = Settings(
            environment=Environment.PRODUCTION,
            _env_file=None,
        )
        assert prod_settings.is_dev is False

    def test_app_debug_wired_to_settings(self) -> None:
        """app.debug is set from settings.is_dev at module level."""
        from course_supporter.config import settings

        # app.debug is set once at FastAPI() creation from settings.is_dev
        assert app.debug is settings.is_dev
