"""Redis cache wrapper for cost-report endpoints (KD5).

Five-minute TTL — staleness window is documented; per-write cache
invalidation is deferred (over-engineering for the TTL). Stores the
serialised JSON of the response body (Pydantic ``model_dump_json
(by_alias=True)``); callers re-hydrate via ``model_validate_json``.

Module-level functions rather than a class — no hidden state, simple
test ergonomics, explicit Redis client passing for FastAPI DI.

Cache key shapes:
    cost:summary:{tenant_id}:{from}:{to}:{lc}:{oc}:{lp}:{op}
    cost:course:{tenant_id}:{course_node_id}:{from}:{to}:{ln}:{on}:{la}:{oa}

Argument order in :func:`make_summary_key` and :func:`make_course_key`
matches the natural reading order of the cache key string. **Never
re-order silently** — a key-shape mismatch between writer and reader
produces silent cache misses (always-miss) without a runtime error.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from arq.connections import ArqRedis

TTL_SECONDS = 300


def make_summary_key(
    *,
    tenant_id: UUID,
    from_date: date,
    to_date: date,
    limit_courses: int,
    offset_courses: int,
    limit_providers: int,
    offset_providers: int,
) -> str:
    """Build cache key for ``GET /api/v1/cost/summary``."""
    return (
        f"cost:summary:{tenant_id}:{from_date}:{to_date}"
        f":{limit_courses}:{offset_courses}"
        f":{limit_providers}:{offset_providers}"
    )


def make_course_key(
    *,
    tenant_id: UUID,
    course_node_id: UUID,
    from_date: date,
    to_date: date,
    limit_nodes: int,
    offset_nodes: int,
    limit_actions: int,
    offset_actions: int,
) -> str:
    """Build cache key for ``GET /api/v1/cost/course/{course_node_id}``."""
    return (
        f"cost:course:{tenant_id}:{course_node_id}:{from_date}:{to_date}"
        f":{limit_nodes}:{offset_nodes}"
        f":{limit_actions}:{offset_actions}"
    )


async def get_cached(redis: ArqRedis, key: str) -> str | None:
    """Read a cached JSON string from Redis. Returns ``None`` on miss."""
    value = await redis.get(key)
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def set_cached(redis: ArqRedis, key: str, value: str) -> None:
    """Write a JSON string to Redis with the standard cost-report TTL."""
    await redis.set(key, value, ex=TTL_SECONDS)
