"""Rate limits kept in Redis, shared by every API process (O-1, R9).

The process-local :class:`~course_supporter.auth.rate_limiter.InMemoryRateLimiter`
counts per process (DD-6-C): with several workers a client gets the limit once
per worker. The public enrollment routes count in Redis instead, so the limit
holds across processes.

Counting:
    A fixed window per (limit name, subject): the first request of a window
    creates the counter with the window's TTL, every request adds one, and a
    request that takes the counter past the limit is refused with the seconds
    left in the window (``Retry-After``). Set-if-absent, increment and TTL run
    in one ``MULTI`` — a counter never outlives its window.

Privacy:
    The subject (an IP, an email, a course key, an id) is never stored as is:
    the Redis key carries its SHA-256, so neither Redis nor a key dump shows an
    address or an email.

Redis unavailable:
    The request passes and a WARNING is logged (``rate_limit_unavailable``):
    enrollment is not blocked by the limiter's own failure.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Final

import structlog
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from course_supporter.config import RateLimit

logger = structlog.get_logger()

KEY_PREFIX: Final = "rl:"
"""Every counter's Redis key starts with it."""

TIMEOUT_S: Final = 0.5
"""How long a check waits for Redis before letting the request through."""


class RateLimitedError(Exception):
    """A request over its limit; ``retry_after`` seconds until the window ends."""

    def __init__(self, name: str, retry_after: int) -> None:
        super().__init__(f"rate limit {name} exceeded")
        self.name = name
        self.retry_after = retry_after


def counter_key(name: str, subject: str) -> str:
    """The Redis key of one counter: the limit's name and the subject's hash."""
    digest = hashlib.sha256(subject.encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}{name}:{digest}"


class RedisRateLimiter:
    """Fixed-window counters in Redis (module docstring)."""

    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis

    async def hit(self, name: str, subject: str, rule: RateLimit) -> None:
        """Count one request of ``subject`` against the limit ``name``.

        Raises:
            RateLimitedError: the request is over ``rule``.
        """
        if self._redis is None:
            logger.warning("rate_limit_unavailable", limit=name, error="no_redis")
            return
        key = counter_key(name, subject)
        try:
            count, ttl = await asyncio.wait_for(
                self._count(key, rule.window_s), timeout=TIMEOUT_S
            )
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "rate_limit_unavailable", limit=name, error=type(exc).__name__
            )
            return
        if count > rule.limit:
            raise RateLimitedError(name, max(ttl, 1))

    async def _count(self, key: str, window_s: int) -> tuple[int, int]:
        redis = self._redis
        assert redis is not None  # checked by hit()
        async with redis.pipeline(transaction=True) as pipe:
            pipe.set(key, 0, ex=window_s, nx=True)
            pipe.incr(key)
            pipe.ttl(key)
            _, count, ttl = await pipe.execute()
        return int(count), int(ttl)
