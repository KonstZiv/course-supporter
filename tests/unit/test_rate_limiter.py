"""Tests for rate limiting (PD-005)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.auth.rate_limiter import InMemoryRateLimiter
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import CourseRepository


class TestInMemoryRateLimiter:
    def test_allows_under_limit(self) -> None:
        """Requests under limit are all allowed."""
        limiter = InMemoryRateLimiter(window_seconds=60)
        for _ in range(5):
            allowed, retry_after = limiter.check("tenant:prep", limit=10)
            assert allowed is True
            assert retry_after == 0

    def test_blocks_over_limit(self) -> None:
        """Request exceeding limit is blocked."""
        limiter = InMemoryRateLimiter(window_seconds=60)
        for _ in range(10):
            allowed, _retry = limiter.check("tenant:prep", limit=10)
            assert allowed is True

        allowed, retry_after = limiter.check("tenant:prep", limit=10)
        assert allowed is False
        assert retry_after > 0

    def test_retry_after_positive(self) -> None:
        """Blocked request returns positive retry_after."""
        limiter = InMemoryRateLimiter(window_seconds=60)
        for _ in range(5):
            limiter.check("tenant:prep", limit=5)

        allowed, retry_after = limiter.check("tenant:prep", limit=5)
        assert allowed is False
        assert retry_after >= 1

    def test_window_expires(self) -> None:
        """After window expires, old requests are not counted."""
        limiter = InMemoryRateLimiter(window_seconds=60)
        base_time = 1000.0

        with patch("course_supporter.auth.rate_limiter.time.monotonic") as mock_time:
            # Fill up the limit at t=1000
            mock_time.return_value = base_time
            for _ in range(5):
                limiter.check("tenant:prep", limit=5)

            # Should be blocked at t=1000
            allowed, _retry = limiter.check("tenant:prep", limit=5)
            assert allowed is False

            # Advance past the window (60s)
            mock_time.return_value = base_time + 61.0

            # Should be allowed again
            allowed, retry_after = limiter.check("tenant:prep", limit=5)
            assert allowed is True
            assert retry_after == 0

    def test_different_keys_independent(self) -> None:
        """Different tenants have independent rate limits."""
        limiter = InMemoryRateLimiter(window_seconds=60)

        # Fill tenant_a
        for _ in range(5):
            limiter.check("tenant_a:prep", limit=5)
        allowed_a, _ = limiter.check("tenant_a:prep", limit=5)
        assert allowed_a is False

        # tenant_b should still be allowed
        allowed_b, retry_after = limiter.check("tenant_b:prep", limit=5)
        assert allowed_b is True
        assert retry_after == 0

    def test_cleanup_removes_expired(self) -> None:
        """Cleanup removes keys with all expired entries."""
        limiter = InMemoryRateLimiter(window_seconds=60)
        base_time = 1000.0

        with patch("course_supporter.auth.rate_limiter.time.monotonic") as mock_time:
            mock_time.return_value = base_time
            limiter.check("tenant:prep", limit=10)
            limiter.check("tenant:check", limit=10)

            # Advance past the window
            mock_time.return_value = base_time + 61.0

            cleaned = limiter.cleanup()
            assert cleaned == 2

        # Verify internal state is clean
        assert len(limiter._requests) == 0


class TestRateLimitAPI:
    @pytest.mark.asyncio
    async def test_429_response_in_api(self) -> None:
        """Exceeding rate limit returns 429 with Retry-After header."""
        tenant = TenantContext(
            tenant_id=uuid.uuid4(),
            tenant_name="test-tenant",
            scopes=["prep"],
            rate_limit_prep=2,
            rate_limit_check=1000,
            key_prefix="cs_test",
        )

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: tenant

        # Reset the global rate limiter to avoid state leakage
        from course_supporter.auth.scopes import rate_limiter

        rate_limiter._requests.clear()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                course = MagicMock()
                course.id = uuid.uuid4()
                course.title = "Test"
                course.description = None
                course.created_at = datetime.now(UTC)
                course.updated_at = datetime.now(UTC)

                with patch.object(CourseRepository, "create", return_value=course):
                    # First 2 requests should pass (limit=2)
                    r1 = await client.post(
                        "/api/v1/courses",
                        json={"title": "Course 1"},
                    )
                    assert r1.status_code == 201

                    r2 = await client.post(
                        "/api/v1/courses",
                        json={"title": "Course 2"},
                    )
                    assert r2.status_code == 201

                    # Third request should be rate limited
                    r3 = await client.post(
                        "/api/v1/courses",
                        json={"title": "Course 3"},
                    )
                    assert r3.status_code == 429
                    assert r3.json()["detail"] == "Rate limit exceeded"
                    assert "retry-after" in r3.headers
                    assert int(r3.headers["retry-after"]) >= 1
        finally:
            rate_limiter._requests.clear()
            app.dependency_overrides.clear()
