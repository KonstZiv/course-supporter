"""Unit tests for ``services.cost_cache`` (KD5 cost-report cache helpers).

Pure helpers — only the bytes-vs-str decode path on ``get_cached``
needs an AsyncMock for the Redis client. Key builders are exercised
for both shape and uniqueness so an accidental re-ordering of
positional segments is caught.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock

import pytest

from course_supporter.services.cost_cache import (
    TTL_SECONDS,
    get_cached,
    make_course_key,
    make_summary_key,
    set_cached,
)


class TestTTLConstant:
    def test_ttl_is_five_minutes(self) -> None:
        assert TTL_SECONDS == 300


class TestMakeSummaryKey:
    def test_shape_and_separators(self) -> None:
        tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        key = make_summary_key(
            tenant_id=tenant_id,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 4, 28),
            limit_courses=50,
            offset_courses=0,
            limit_providers=100,
            offset_providers=10,
        )
        assert key == (f"cost:summary:{tenant_id}:2026-01-01:2026-04-28:50:0:100:10")

    def test_distinct_pagination_yields_distinct_keys(self) -> None:
        common = {
            "tenant_id": uuid.uuid4(),
            "from_date": date(2026, 1, 1),
            "to_date": date(2026, 4, 28),
        }
        a = make_summary_key(
            **common,
            limit_courses=50,
            offset_courses=0,
            limit_providers=50,
            offset_providers=0,
        )
        b = make_summary_key(
            **common,
            limit_courses=100,
            offset_courses=0,
            limit_providers=50,
            offset_providers=0,
        )
        assert a != b


class TestMakeCourseKey:
    def test_shape_includes_course_node_id(self) -> None:
        tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        course_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        key = make_course_key(
            tenant_id=tenant_id,
            course_node_id=course_id,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 4, 28),
            limit_nodes=50,
            offset_nodes=0,
            limit_actions=50,
            offset_actions=0,
        )
        assert key == (
            f"cost:course:{tenant_id}:{course_id}:2026-01-01:2026-04-28:50:0:50:0"
        )


class TestGetCached:
    async def test_returns_none_on_miss(self) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        assert await get_cached(redis, "k") is None

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (b'{"x":1}', '{"x":1}'),
            ('{"x":1}', '{"x":1}'),
        ],
    )
    async def test_decodes_bytes_or_passes_str(
        self, payload: bytes | str, expected: str
    ) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=payload)
        assert await get_cached(redis, "k") == expected


class TestSetCached:
    async def test_writes_with_ttl(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock()
        await set_cached(redis, "k", "v")
        redis.set.assert_awaited_once_with("k", "v", ex=TTL_SECONDS)
