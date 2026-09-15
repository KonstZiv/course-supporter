"""Tests for FastAPI bootstrap: health, CORS, error handling."""

import re
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.config import settings
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


class HealthMocks(NamedTuple):
    """Mocks returned by mock_health_deps context manager."""

    db_session: AsyncMock
    s3_client: AsyncMock
    redis_client: AsyncMock


@contextmanager
def mock_health_deps(
    *,
    db_error: Exception | None = None,
    s3_error: Exception | None = None,
    redis_error: Exception | None = None,
) -> Generator[HealthMocks]:
    """Mock DB, S3 and Redis dependencies for health check tests.

    Args:
        db_error: If set, async_session __aenter__ raises this exception.
        s3_error: If set, s3_client.check_connectivity raises this exception.
        redis_error: If set, arq_redis.ping raises this exception.
    """
    mock_s3 = AsyncMock()
    if s3_error:
        mock_s3.check_connectivity = AsyncMock(side_effect=s3_error)
    else:
        mock_s3.check_connectivity = AsyncMock()

    mock_redis = AsyncMock()
    if redis_error:
        mock_redis.ping = AsyncMock(side_effect=redis_error)
    else:
        mock_redis.ping = AsyncMock(return_value=True)

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
        app.state.arq_redis = mock_redis

        yield HealthMocks(
            db_session=mock_db_session, s3_client=mock_s3, redis_client=mock_redis
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


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_all_ok(self, client: AsyncClient) -> None:
        """GET /health returns 200 when DB, S3 and Redis are reachable."""
        with mock_health_deps():
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["db"] == "ok"
        assert data["checks"]["s3"] == "ok"
        assert data["checks"]["redis"] == "ok"
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
        assert data["checks"]["redis"] == "ok"

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
        assert data["checks"]["redis"] == "ok"

    @pytest.mark.asyncio
    async def test_health_redis_down(self, client: AsyncClient) -> None:
        """GET /health returns 503 when Redis is unreachable."""
        with mock_health_deps(redis_error=ConnectionError("redis down")):
            response = await client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["checks"]["db"] == "ok"
        assert data["checks"]["s3"] == "ok"
        assert "error" in data["checks"]["redis"]

    @pytest.mark.asyncio
    async def test_health_redis_timeout(self, client: AsyncClient) -> None:
        """GET /health returns 503 when Redis times out."""
        with mock_health_deps(redis_error=TimeoutError("redis timeout")):
            response = await client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert "TimeoutError" in data["checks"]["redis"]

    @pytest.mark.asyncio
    async def test_health_all_down(self, client: AsyncClient) -> None:
        """GET /health returns 503 when all services are down."""
        with mock_health_deps(
            db_error=TimeoutError("db"),
            s3_error=ConnectionError("s3"),
            redis_error=ConnectionError("redis"),
        ):
            response = await client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert "error" in data["checks"]["db"]
        assert "error" in data["checks"]["s3"]
        assert "error" in data["checks"]["redis"]

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
        from course_supporter.storage.course_node_repository import (
            CourseNodeRepository,
        )

        with (
            patch.object(CourseNodeRepository, "list_roots", return_value=[]),
            patch.object(CourseNodeRepository, "count_roots", return_value=0),
        ):
            response = await client.get("/api/v1/nodes")
        assert response.status_code == 200


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


def _without_safety_ladder(raw: dict[str, Any]) -> str:
    """A shape fault: ``load_path_config`` rejects it."""
    del raw["stages"]["safety"]["ladder"]
    return "stages.safety.ladder"


def _safety_rung_pinned_above_its_ceiling(raw: dict[str, Any]) -> str:
    """A valid shape that only ``validate_path_config`` rejects."""
    ceiling = raw["stages"]["safety"]["ceilings"]["output_tokens"]
    raw["stages"]["safety"]["ladder"][0]["max_output_tokens"] = ceiling * 2
    return (
        f"Stage 'safety' rung 0 pins max_output_tokens={ceiling * 2} above the "
        f"stage output ceiling {ceiling}"
    )


def _holey_paths_file(
    tmp_path: Path, make_hole: Callable[[dict[str, Any]], str]
) -> tuple[Path, str]:
    """A copy of the real submission-path file with one hole in it.

    Returns the copy and the text the startup error must carry.
    """
    raw = yaml.safe_load(
        settings.submission_paths_config_path.read_text(encoding="utf-8")
    )
    expected = make_hole(raw)
    path = tmp_path / "submission_paths.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path, expected


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_disposes_engine(self) -> None:
        """Lifespan disposes engine on shutdown."""
        mock_arq = AsyncMock()
        with (
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "course_supporter.api.app.create_pool",
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_hole",
        [_without_safety_ladder, _safety_rung_pinned_above_its_ceiling],
        ids=["no-ladder", "pin-above-output-ceiling"],
    )
    async def test_lifespan_refuses_a_submission_path_with_a_hole(
        self, tmp_path: Path, make_hole: Callable[[dict[str, Any]], str]
    ) -> None:
        """A hole in the submission-path file stops the app booting.

        One hole per step of the startup check (mentor-rebuild 02): a missing
        ladder fails the shape (``load_path_config``); a pin above the stage's
        output ceiling passes the shape and fails only ``validate_path_config``.
        Everything after the check is mocked as in
        ``test_lifespan_disposes_engine``, so without the check the lifespan
        would start and this test would fail.
        """
        broken, expected = _holey_paths_file(tmp_path, make_hole)
        mock_arq = AsyncMock()
        with (
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "course_supporter.api.app.create_pool",
                new_callable=AsyncMock,
                return_value=mock_arq,
            ),
            patch.object(settings, "submission_paths_config_path", broken),
        ):
            mock_engine.dispose = AsyncMock()
            mock_s3_cls.return_value = AsyncMock()

            from course_supporter.api.app import lifespan

            with pytest.raises(ValueError, match=re.escape(expected)):
                async with lifespan(app):
                    pass

    @pytest.mark.asyncio
    async def test_lifespan_pool_overrides_expires_extra_ms(self) -> None:
        """API-side pool is created with the DD-3.3c-I expires_extra_ms (ms)."""
        from course_supporter.config import get_settings

        expected_ms = get_settings().intake_job_expires_ms
        mock_arq = AsyncMock()
        with (
            patch("course_supporter.api.app.engine") as mock_engine,
            patch("course_supporter.api.app.S3Client") as mock_s3_cls,
            patch(
                "course_supporter.api.app.create_pool",
                new_callable=AsyncMock,
                return_value=mock_arq,
            ) as mock_create_pool,
        ):
            mock_engine.dispose = AsyncMock()
            mock_s3 = AsyncMock()
            mock_s3_cls.return_value = mock_s3

            from course_supporter.api.app import lifespan

            async with lifespan(app):
                pass

        mock_create_pool.assert_awaited_once()
        assert mock_create_pool.await_args.kwargs["expires_extra_ms"] == expected_ms
        assert mock_create_pool.await_args.kwargs["expires_extra_ms"] == 345_600_000
