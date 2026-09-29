"""The tests' own Redis database — never the one the app and worker use.

Integration tests enqueue real ARQ jobs (one of them deferred by an hour). If
they landed in the database a local worker reads, a worker started with real
provider keys would pick them up and make paid model calls. So every test that
touches Redis goes through this module: same address as the app, its own
database number, emptied before and after use.

The number is set in one place — ``TEST_REDIS_DB`` (default ``15``, the last of
Redis's 16 default databases, as far as possible from the app's ``0``).
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from arq.connections import ArqRedis, RedisSettings, create_pool

from course_supporter.config import get_settings

TEST_REDIS_DB_ENV = "TEST_REDIS_DB"
DEFAULT_TEST_REDIS_DB = 15


class RedisTestDbIsAppDbError(RuntimeError):
    """The tests' Redis database is the one the app and worker use."""


def app_redis_settings() -> RedisSettings:
    """Where the app and the worker read and write jobs (``REDIS_URL``)."""
    return RedisSettings.from_dsn(get_settings().redis_url)


def redis_test_db() -> int:
    """The tests' database number: ``TEST_REDIS_DB`` or the default."""
    return int(os.environ.get(TEST_REDIS_DB_ENV, DEFAULT_TEST_REDIS_DB))


def ensure_not_app_redis(test: RedisSettings, app: RedisSettings) -> None:
    """Refuse a test database that is the app's own (lock 1)."""
    same_server = (test.host, test.port, test.unix_socket_path) == (
        app.host,
        app.port,
        app.unix_socket_path,
    )
    if same_server and test.database == app.database:
        raise RedisTestDbIsAppDbError(
            f"Redis tests refuse to run: the test database {test.database} on "
            f"{test.host}:{test.port} is the one the app and worker use "
            f"(REDIS_URL). Jobs enqueued there would be taken by a local "
            f"worker and could make paid model calls. Set {TEST_REDIS_DB_ENV} "
            f"to a database number other than {app.database}."
        )


def redis_test_settings() -> RedisSettings:
    """The app's Redis address with the tests' database number, checked."""
    app = app_redis_settings()
    test = dataclasses.replace(app, database=redis_test_db())
    ensure_not_app_redis(test, app)
    return test


def pool_database(pool: ArqRedis) -> int:
    """The database number a live pool is actually connected to."""
    return int(pool.connection_pool.connection_kwargs.get("db", 0))


@asynccontextmanager
async def open_test_arq_redis() -> AsyncGenerator[ArqRedis]:
    """A pool on the tests' database, emptied before and after use (lock 2).

    The pool's real database is checked against the app's before anything is
    deleted, so the cleanup can only ever reach the tests' database.
    """
    settings = redis_test_settings()
    pool = await create_pool(settings)
    try:
        ensure_not_app_redis(
            dataclasses.replace(settings, database=pool_database(pool)),
            app_redis_settings(),
        )
        await pool.flushdb()
        try:
            yield pool
        finally:
            await pool.flushdb()
    finally:
        await pool.aclose()
