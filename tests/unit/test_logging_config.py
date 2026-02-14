"""Tests for structured logging configuration and request middleware."""

import json
import logging
from io import StringIO
from unittest.mock import patch

import pytest
import structlog
from httpx import ASGITransport, AsyncClient

from course_supporter.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Reset structlog state after each test."""
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)


def _capture_log_output(environment: str, log_level: str = "DEBUG") -> str:
    """Configure logging, emit a message, return captured output."""
    configure_logging(environment=environment, log_level=log_level)

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.getLogger().handlers[0].formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)

    logger = structlog.get_logger()
    logger.info("test_event", key="value")

    return stream.getvalue()


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_production_json(self) -> None:
        """Production environment produces valid JSON output."""
        output = _capture_log_output("production")
        parsed = json.loads(output)
        assert parsed["event"] == "test_event"
        assert parsed["key"] == "value"
        assert parsed["level"] == "info"

    def test_configure_development_console(self) -> None:
        """Development environment produces human-readable console output."""
        output = _capture_log_output("development")
        assert "test_event" in output
        assert "key=value" in output or "key" in output

    def test_configure_sets_log_level(self) -> None:
        """Root logger level is set to the specified value."""
        configure_logging(log_level="WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_configure_includes_timestamp(self) -> None:
        """Production JSON output contains ISO timestamp."""
        output = _capture_log_output("production")
        parsed = json.loads(output)
        assert "timestamp" in parsed
        # ISO format contains 'T' separator
        assert "T" in parsed["timestamp"]


class TestRequestLoggingMiddleware:
    """Tests for HTTP request logging middleware."""

    @pytest.fixture()
    def test_app(self):
        """Create a minimal FastAPI app with middleware for isolated testing."""
        from fastapi import FastAPI

        from course_supporter.api.middleware import RequestLoggingMiddleware

        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.get("/test-endpoint")
        async def _test_endpoint() -> dict[str, str]:
            return {"ok": "true"}

        @app.get("/health")
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/docs")
        async def _docs() -> str:
            return "docs"

        return app

    async def test_middleware_logs_request(self, test_app) -> None:
        """Middleware logs method, path, status_code, latency_ms."""
        with patch("course_supporter.api.middleware.logger") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url="http://test",
            ) as client:
                await client.get("/test-endpoint")

            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args
            assert call_args[0][0] == "http_request"
            assert call_args[1]["method"] == "GET"
            assert call_args[1]["path"] == "/test-endpoint"
            assert call_args[1]["status_code"] == 200
            assert "latency_ms" in call_args[1]

    async def test_middleware_skips_health(self, test_app) -> None:
        """Middleware does not log requests to /health."""
        with patch("course_supporter.api.middleware.logger") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url="http://test",
            ) as client:
                await client.get("/health")

            mock_logger.info.assert_not_called()

    async def test_middleware_skips_docs(self, test_app) -> None:
        """Middleware does not log requests to /docs."""
        with patch("course_supporter.api.middleware.logger") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url="http://test",
            ) as client:
                await client.get("/docs")

            mock_logger.info.assert_not_called()
