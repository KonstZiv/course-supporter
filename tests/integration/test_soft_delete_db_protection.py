"""Integration: DB-level soft-delete protection trigger + repository.

Marked ``requires_db`` — exercises the BEFORE UPDATE trigger and the
partial-index/find-flavour behaviour against a live PostgreSQL.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Tenant
from course_supporter.storage.repositories import SoftDeleteRepository

pytestmark = pytest.mark.requires_db


class TenantRepository(SoftDeleteRepository[Tenant]):
    model = Tenant


async def _make_active_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"trigger-test-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


# ── Trigger behaviour ─────────────────────────────────────────────


class TestSoftDeleteTrigger:
    async def test_update_regular_field_on_deleted_row_raises(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_active_tenant(db_session)
        repo = TenantRepository(db_session)
        await repo.soft_delete(tenant.id)

        with pytest.raises(DBAPIError) as excinfo:
            await db_session.execute(
                text("UPDATE tenants SET name = :n WHERE id = :id"),
                {"n": "renamed", "id": tenant.id},
            )
        assert "soft-deleted" in str(excinfo.value).lower()

    async def test_update_only_deleted_at_succeeds(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_active_tenant(db_session)
        repo = TenantRepository(db_session)
        await repo.soft_delete(tenant.id)

        # Un-delete via raw SQL — only deleted_at changes.
        await db_session.execute(
            text("UPDATE tenants SET deleted_at = NULL WHERE id = :id"),
            {"id": tenant.id},
        )
        await db_session.refresh(tenant)
        assert tenant.deleted_at is None


# ── Repository find_* against real DB ─────────────────────────────


class TestRepositoryAgainstDB:
    async def test_find_active_excludes_deleted(self, db_session: AsyncSession) -> None:
        repo = TenantRepository(db_session)
        active = await _make_active_tenant(db_session)
        deleted = await _make_active_tenant(db_session)
        await repo.soft_delete(deleted.id)

        rows = await repo.find_active()
        ids = {r.id for r in rows}
        assert active.id in ids
        assert deleted.id not in ids

    async def test_find_only_deleted_returns_only_deleted(
        self, db_session: AsyncSession
    ) -> None:
        repo = TenantRepository(db_session)
        active = await _make_active_tenant(db_session)
        deleted = await _make_active_tenant(db_session)
        await repo.soft_delete(deleted.id)

        rows = await repo.find_only_deleted()
        ids = {r.id for r in rows}
        assert deleted.id in ids
        assert active.id not in ids

    async def test_find_including_deleted_returns_both(
        self, db_session: AsyncSession
    ) -> None:
        repo = TenantRepository(db_session)
        active = await _make_active_tenant(db_session)
        deleted = await _make_active_tenant(db_session)
        await repo.soft_delete(deleted.id)

        rows = await repo.find_including_deleted()
        ids = {r.id for r in rows}
        assert {active.id, deleted.id}.issubset(ids)

    async def test_soft_delete_idempotent_on_real_db(
        self, db_session: AsyncSession
    ) -> None:
        """Second call must NOT issue an UPDATE — the trigger would block it."""
        repo = TenantRepository(db_session)
        tenant = await _make_active_tenant(db_session)

        first = await repo.soft_delete(tenant.id)
        assert first is not None
        first_ts = first.deleted_at
        assert first_ts is not None

        # If repository early-return is broken, this would raise via trigger.
        second = await repo.soft_delete(tenant.id)
        assert second is not None
        assert second.deleted_at == first_ts
