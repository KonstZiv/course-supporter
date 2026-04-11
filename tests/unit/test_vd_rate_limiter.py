"""Tests for VD rate limiter and retry helpers."""

from __future__ import annotations

import asyncio
import time

import pytest

from course_supporter.vd.rate_limiter import (
    VDRateLimiter,
    is_rate_limit,
    retry_wait,
)


class TestIsRateLimit:
    def test_429_string(self) -> None:
        assert is_rate_limit(Exception("429 Resource exhausted"))

    def test_resource_exhausted(self) -> None:
        assert is_rate_limit(Exception("RESOURCE_EXHAUSTED"))

    def test_other_error(self) -> None:
        assert not is_rate_limit(Exception("500 Internal server error"))

    def test_empty_message(self) -> None:
        assert not is_rate_limit(Exception(""))


class TestRetryWait:
    def test_extracts_retry_delay(self) -> None:
        exc = Exception("Please retry in 12.5s")
        wait = retry_wait(exc, 0)
        assert wait == pytest.approx(14.5)  # 12.5 + 2.0 buffer

    def test_exponential_backoff(self) -> None:
        exc = Exception("429 no retry hint")
        w0 = retry_wait(exc, 0)
        w1 = retry_wait(exc, 1)
        w2 = retry_wait(exc, 2)
        assert w1 == pytest.approx(w0 * 2)
        assert w2 == pytest.approx(w0 * 4)

    def test_base_wait_is_40(self) -> None:
        exc = Exception("429 no hint")
        assert retry_wait(exc, 0) == pytest.approx(40.0)


class TestVDRateLimiter:
    async def test_acquire_respects_interval(self) -> None:
        limiter = VDRateLimiter(rpm=600)  # 10 per sec → 0.1s interval
        t0 = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.09  # ~0.1s interval enforced

    async def test_zero_rpm_no_wait(self) -> None:
        limiter = VDRateLimiter(rpm=0)
        t0 = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05

    async def test_serializes_concurrent_calls(self) -> None:
        limiter = VDRateLimiter(rpm=120)  # 2 per sec → 0.5s interval
        timestamps: list[float] = []

        async def call() -> None:
            await limiter.acquire()
            timestamps.append(time.monotonic())

        await asyncio.gather(call(), call(), call())
        assert len(timestamps) == 3
        # Each call should be at least ~0.5s apart
        for i in range(1, len(timestamps)):
            assert timestamps[i] - timestamps[i - 1] >= 0.4
