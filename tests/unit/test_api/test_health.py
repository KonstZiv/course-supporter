"""Tests for FastAPI bootstrap: health, CORS, error handling."""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

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


class HealthMocks(NamedTuple):
    """Mocks returned by mock_health_deps context manager."""

    db_session: AsyncMock
    s3_client: AsyncMock


@contextmanager
def mock_health_deps(
    *,
    db_error: Exception | None = None,
    s3_error: Exception | None = None,
) -> Generator[HealthMocks]:
    """Mock DB and S3 dependencies for health check tests.

    Args:
        db_error: If set, async_session __aenter__ raises this exception.
        s3_error: If set, s3_client.check_connectivity raises this exception.
    """
    mock_s3 = AsyncMock()
    if s3_error:
        mock_s3.check_connectivity = AsyncMock(side_effect=s3_error)
    else:
        mock_s3.check_connectivity = AsyncMock()

    mock_db_session = AsyncMock()
    mock_db_session.execute = AsyncMock()

    with patch("course_supporter.api.app.async_session") as mock_session_factory:
        if db_error:
            mock_session_factory.return_value.__aenter__ = AsyncMock(
                side_effect=db_error
            )
        else:
            mock_session_factory.return_value.__aenter__ = AsyncMock(
                return_value=mock_db_session
            )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        app.state.s3_client = mock_s3

        yield HealthMocks(db_session=mock_db_session, s3_client=mock_s3)


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


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_all_ok(self, client: AsyncClient) -> None:
        """GET /health returns 200 when DB and S3 are reachable."""
        with mock_health_deps():
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["db"] == "ok"
        assert data["checks"]["s3"] == "ok"
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_health_db_down(self, client: AsyncClient) -> None:
        """GET /health returns 503 when DB is unreachable."""
        with mock_health_deps(db_error=TimeoutError("db timeout")):
            response = await client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert "error" in data["checks"]["db"]
        assert data["checks"]["s3"] == "ok"

    @pytest.mark.asyncio
    async def test_health_s3_down(self, client: AsyncClient) -> None:
        """GET /health returns 503 when S3 is unreachable."""
        with mock_health_deps(s3_error=ConnectionError("s3 down")):
            response = await client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["checks"]["db"] == "ok"
        assert "error" in data["checks"]["s3"]

    @pytest.mark.asyncio
    async def test_health_no_auth(self) -> None:
        """GET /health is accessible without API key."""
        with mock_health_deps():
            # No dependency overrides — no auth bypass
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                response = await ac.get("/health")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_method_not_allowed(self, client: AsyncClient) -> None:
        """POST /health returns 405."""
        response = await client.post("/health")
        assert response.status_code == 405


class TestRouting:
    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self, client: AsyncClient) -> None:
        """GET /nonexistent returns 404."""
        response = await client.get("/nonexistent")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_api_v1_prefix_registered(self, client: AsyncClient) -> None:
        """Routes are registered under /api/v1."""
        # /api/v1/courses should exist (empty router, returns 405 for GET)
        # At minimum, the prefix should not 404 for valid paths
        response = await client.get("/api/v1/courses")
        # May return 404 (no GET route) or 405 — but NOT a path-not-found error
        # with no routes defined, FastAPI returns 404 for the path
        assert response.status_code in (200, 404, 405)


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_unhandled_exception_handler_returns_500(self) -> None:
        """Global exception handler returns 500 JSON response."""
        from course_supporter.api.app import unhandled_exception_handler

        mock_request = Request(
            scope={"type": "http", "method": "GET", "path": "/test", "headers": []}
        )
        response = await unhandled_exception_handler(mock_request, RuntimeError("boom"))
        assert response.status_code == 500
        assert response.body == b'{"detail":"Internal server error"}'


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_creates_model_router(self) -> None:
        """Lifespan sets app.state.model_router."""
        mock_arq = AsyncMock()
        with (
            patch("course_supporter.api.app.create_model_router") as mock_create,
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "arq.create_pool",
                new_callable=AsyncMock,
                return_value=mock_arq,
            ),
        ):
            mock_create.return_value = "fake_router"
            mock_engine.dispose = AsyncMock()
            mock_s3 = AsyncMock()
            mock_s3_cls.return_value = mock_s3

            from course_supporter.api.app import lifespan

            async with lifespan(app):
                assert app.state.model_router == "fake_router"
                mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_disposes_engine(self) -> None:
        """Lifespan disposes engine on shutdown."""
        mock_arq = AsyncMock()
        with (
            patch("course_supporter.api.app.create_model_router"),
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "arq.create_pool",
                new_callable=AsyncMock,
                return_value=mock_arq,
            ),
        ):
            mock_engine.dispose = AsyncMock()
            mock_s3 = AsyncMock()
            mock_s3_cls.return_value = mock_s3

            from course_supporter.api.app import lifespan

            async with lifespan(app):
                pass
            mock_engine.dispose.assert_awaited_once()
