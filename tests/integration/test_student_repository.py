"""Integration tests for StudentRepository portal additions (Phase 6 T1).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``--run-db``.
Covers the explicit ``create`` (provisioning) + ``set_display_name`` paths.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Tenant
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


class TestCreate:
    async def test_create_persists_with_display_name(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentRepository(db_session)
        student = await repo.create(
            tenant_id=seed_tenant.id,
            external_id="ext-portal-1",
            display_name="Alice",
        )
        assert student.id is not None
        assert student.external_id == "ext-portal-1"
        assert student.display_name == "Alice"

        fetched = await repo.get_by_id(student.id)
        assert fetched is not None
        assert fetched.display_name == "Alice"

    async def test_duplicate_external_id_raises(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentRepository(db_session)
        await repo.create(tenant_id=seed_tenant.id, external_id="dup-ext")
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await repo.create(tenant_id=seed_tenant.id, external_id="dup-ext")


class TestSetDisplayName:
    async def test_set_display_name_updates(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentRepository(db_session)
        student = await repo.create(tenant_id=seed_tenant.id, external_id="ext-rename")
        assert student.display_name is None

        await repo.set_display_name(student.id, "Bob")
        await db_session.refresh(student)
        assert student.display_name == "Bob"

    async def test_set_display_name_unknown_student_noop(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentRepository(db_session)
        # No row matches — UPDATE affects nothing, no error.
        await repo.set_display_name(uuid.uuid4(), "Nobody")
