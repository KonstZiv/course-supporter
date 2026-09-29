"""The tests' Redis database, proven on a live Redis.

Lock 1: the ``arq_redis`` fixture is connected to the tests' database, never
to the one the app and worker use. Lock 2: jobs enqueued through it — even one
deferred by an hour — are gone once the test is over.

Run with: ``uv run pytest tests/integration/test_redis_isolation.py --run-redis``
"""

from __future__ import annotations

import pytest
from arq.connections import ArqRedis, create_pool

from tests._helpers.redis_isolation import (
    app_redis_settings,
    open_test_arq_redis,
    pool_database,
    redis_test_db,
    redis_test_settings,
)

pytestmark = [pytest.mark.requires_redis]


async def test_lock1_fixture_is_not_on_the_app_database(arq_redis: ArqRedis) -> None:
    assert pool_database(arq_redis) != app_redis_settings().database
    assert pool_database(arq_redis) == redis_test_db()


async def test_lock2_no_arq_keys_left_after_enqueue() -> None:
    async with open_test_arq_redis() as pool:
        job = await pool.enqueue_job("lock2_probe", _defer_by=3600)
        assert job is not None
        assert await pool.keys("arq:*")  # the job really is in Redis

    probe = await create_pool(redis_test_settings())
    try:
        assert await probe.keys("arq:*") == []
    finally:
        await probe.aclose()
