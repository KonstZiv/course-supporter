"""Shared rate limiter and retry helpers for the VD pipeline.

VD makes hundreds of Gemini calls per video (one per frame + memory
updates). This module provides a rate limiter that enforces minimum
intervals between calls, and retry helpers for 429 errors.

Both ``VisualAnalyzer`` and ``MemoryPipeline`` share the same rate
limiter instance to coordinate against a single Gemini quota.
"""

from __future__ import annotations

import asyncio
import re
import time

import structlog

logger = structlog.get_logger()

_RETRY_BASE_WAIT = 40.0  # seconds, matches typical Gemini retry-after


class VDRateLimiter:
    """Async rate limiter that enforces minimum interval between calls.

    Args:
        rpm: Total requests per minute across all API keys.
    """

    def __init__(self, rpm: int) -> None:
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until enough time has passed since the last call."""
        async with self._lock:
            elapsed = time.monotonic() - self._last_call_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_call_time = time.monotonic()


def is_rate_limit(exc: BaseException) -> bool:
    """Check if an exception is a 429 rate-limit error."""
    exc_str = str(exc)
    return "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str


def retry_wait(exc: BaseException, attempt: int) -> float:
    """Calculate retry wait time from error message or exponential backoff."""
    match = re.search(r"retry\s+in\s+([\d.]+)s", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1)) + 2.0  # add buffer
    return _RETRY_BASE_WAIT * float(2**attempt)
