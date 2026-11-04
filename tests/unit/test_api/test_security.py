"""Security hardening tests: CORS, error handling, debug mode, docs visibility."""

import importlib
import sys
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.config import Environment, settings
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = lambda *a, **kw: None
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncGenerator[AsyncClient]:
    """AsyncClient that skips real DB."""
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
            portal_session_secret="prod-secret-override",  # type: ignore[arg-type]
            smtp_host="smtp.example.com",
            smtp_from="noreply@example.com",
            _env_file=None,
        )
        assert prod_settings.is_dev is False


class TestDocsVisibility:
    """Docs and ReDoc endpoints visibility depending on environment."""

    @pytest.mark.asyncio
    async def test_production_hides_docs_and_redoc(self) -> None:
        """Production environment → /docs and /redoc return 404, /openapi.json 200."""
        app_mod = sys.modules["course_supporter.api.app"]
        api_pkg = sys.modules.get("course_supporter.api")
        orig_app = app_mod.app
        try:
            with patch.object(settings, "environment", Environment.PRODUCTION):
                importlib.reload(app_mod)
                prod_app = app_mod.app

                async with AsyncClient(
                    transport=ASGITransport(app=prod_app),
                    base_url="http://test",
                ) as ac:
                    resp_docs = await ac.get("/docs")
                    assert resp_docs.status_code == 404
                    resp_redoc = await ac.get("/redoc")
                    assert resp_redoc.status_code == 404
                    resp_openapi = await ac.get("/openapi.json")
                    assert resp_openapi.status_code == 200
        finally:
            importlib.reload(app_mod)
            app_mod.app = orig_app
            if api_pkg is not None:
                api_pkg.app = orig_app

    @pytest.mark.asyncio
    async def test_development_exposes_docs(self) -> None:
        """Development environment → /docs and /redoc return 200."""
        from course_supporter.api.app import app as dev_app

        async with AsyncClient(
            transport=ASGITransport(app=dev_app),
            base_url="http://test",
        ) as ac:
            resp_docs = await ac.get("/docs")
            assert resp_docs.status_code == 200
            resp_redoc = await ac.get("/redoc")
            assert resp_redoc.status_code == 200
            resp_openapi = await ac.get("/openapi.json")
            assert resp_openapi.status_code == 200
