"""Lock 1: Redis tests refuse to run on the app's own database.

No Redis needed — the refusal happens before any connection is opened.
"""

from __future__ import annotations

import pytest
from arq.connections import RedisSettings

from tests._helpers import redis_isolation
from tests._helpers.redis_isolation import (
    RedisTestDbIsAppDbError,
    ensure_not_app_redis,
    redis_test_settings,
)


def _use_app_redis(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    monkeypatch.setattr(
        redis_isolation, "app_redis_settings", lambda: RedisSettings.from_dsn(dsn)
    )


class TestLock1RefusesAppDatabase:
    def test_same_database_is_refused_with_a_clear_message(self) -> None:
        app = RedisSettings.from_dsn("redis://localhost:6379/0")
        with pytest.raises(RedisTestDbIsAppDbError, match="refuse to run") as err:
            ensure_not_app_redis(
                RedisSettings.from_dsn("redis://localhost:6379/0"), app
            )
        assert "TEST_REDIS_DB" in str(err.value)

    def test_other_database_on_the_same_server_is_allowed(self) -> None:
        app = RedisSettings.from_dsn("redis://localhost:6379/0")
        ensure_not_app_redis(RedisSettings.from_dsn("redis://localhost:6379/15"), app)

    def test_db_given_as_query_parameter_is_still_caught(self) -> None:
        app = RedisSettings.from_dsn("redis://localhost:6379?db=15")
        with pytest.raises(RedisTestDbIsAppDbError):
            ensure_not_app_redis(
                RedisSettings.from_dsn("redis://localhost:6379/15"), app
            )

    def test_test_db_set_to_the_app_db_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_app_redis(monkeypatch, "redis://localhost:6379/4")
        monkeypatch.setenv(redis_isolation.TEST_REDIS_DB_ENV, "4")
        with pytest.raises(RedisTestDbIsAppDbError, match="database 4"):
            redis_test_settings()

    def test_app_on_the_default_test_db_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_app_redis(monkeypatch, "redis://localhost:6379/15")
        monkeypatch.delenv(redis_isolation.TEST_REDIS_DB_ENV, raising=False)
        with pytest.raises(RedisTestDbIsAppDbError):
            redis_test_settings()


class TestTestDatabaseChoice:
    def test_default_is_the_app_address_with_db_15(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_app_redis(monkeypatch, "redis://redis.local:6380/0")
        monkeypatch.delenv(redis_isolation.TEST_REDIS_DB_ENV, raising=False)
        settings = redis_test_settings()
        assert (settings.host, settings.port, settings.database) == (
            "redis.local",
            6380,
            15,
        )

    def test_env_var_picks_the_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_app_redis(monkeypatch, "redis://localhost:6379/0")
        monkeypatch.setenv(redis_isolation.TEST_REDIS_DB_ENV, "9")
        assert redis_test_settings().database == 9
