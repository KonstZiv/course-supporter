"""Unit tests for SoftDeleteRepository (vision §3 KD3, KD12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from course_supporter.storage.orm import Tenant
from course_supporter.storage.repositories import SoftDeleteRepository


class TenantRepository(SoftDeleteRepository[Tenant]):
    """Concrete binding used to exercise the generic base in tests."""

    model = Tenant


def _mock_query_result(rows: list[Any]) -> MagicMock:
    """Build a mock SQLAlchemy result whose ``.scalars().all()`` returns rows."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _compiled_sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


# ── find_active / find_including_deleted / find_only_deleted ──────


class TestFindFilters:
    """Exercise the WHERE-clause shape of each find_* flavour."""

    async def test_find_active_filters_deleted_at_is_null(self) -> None:
        session = AsyncMock()
        session.execute.return_value = _mock_query_result([])
        repo = TenantRepository(session)

        await repo.find_active()

        captured_stmt = session.execute.call_args.args[0]
        assert "deleted_at IS NULL" in _compiled_sql(captured_stmt)

    async def test_find_including_deleted_has_no_deleted_at_filter(self) -> None:
        session = AsyncMock()
        session.execute.return_value = _mock_query_result([])
        repo = TenantRepository(session)

        await repo.find_including_deleted()

        captured_stmt = session.execute.call_args.args[0]
        compiled = _compiled_sql(captured_stmt)
        # `deleted_at` appears in the SELECT list but not in any WHERE clause.
        assert "deleted_at IS" not in compiled

    async def test_find_only_deleted_filters_not_null(self) -> None:
        session = AsyncMock()
        session.execute.return_value = _mock_query_result([])
        repo = TenantRepository(session)

        await repo.find_only_deleted()

        captured_stmt = session.execute.call_args.args[0]
        assert "deleted_at IS NOT NULL" in _compiled_sql(captured_stmt)

    async def test_find_active_returns_session_rows(self) -> None:
        rows = [Tenant(name="active-1"), Tenant(name="active-2")]
        session = AsyncMock()
        session.execute.return_value = _mock_query_result(rows)
        repo = TenantRepository(session)

        result = await repo.find_active()
        assert result == rows


# ── soft_delete ───────────────────────────────────────────────────


class TestSoftDelete:
    """Behaviour of the idempotent ``soft_delete`` setter."""

    async def test_sets_deleted_at_and_flushes(self) -> None:
        entity = Tenant(name="t")
        entity.id = uuid.uuid4()
        entity.deleted_at = None

        session = AsyncMock()
        session.get = AsyncMock(return_value=entity)
        repo = TenantRepository(session)

        before = datetime.now(UTC)
        result = await repo.soft_delete(entity.id)
        after = datetime.now(UTC)

        assert result is entity
        assert entity.deleted_at is not None
        assert before <= entity.deleted_at <= after
        session.flush.assert_awaited_once()

    async def test_idempotent_on_already_deleted(self) -> None:
        already_ts = datetime(2026, 1, 1, tzinfo=UTC)
        entity = Tenant(name="t")
        entity.id = uuid.uuid4()
        entity.deleted_at = already_ts

        session = AsyncMock()
        session.get = AsyncMock(return_value=entity)
        repo = TenantRepository(session)

        result = await repo.soft_delete(entity.id)

        assert result is entity
        assert entity.deleted_at == already_ts  # untouched
        session.flush.assert_not_awaited()

    async def test_returns_none_when_not_found(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        repo = TenantRepository(session)

        result = await repo.soft_delete(uuid.uuid4())

        assert result is None
        session.flush.assert_not_awaited()

    async def test_custom_now_override(self) -> None:
        entity = Tenant(name="t")
        entity.id = uuid.uuid4()
        entity.deleted_at = None
        custom_ts = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

        session = AsyncMock()
        session.get = AsyncMock(return_value=entity)
        repo = TenantRepository(session)

        await repo.soft_delete(entity.id, now=custom_ts)
        assert entity.deleted_at == custom_ts
