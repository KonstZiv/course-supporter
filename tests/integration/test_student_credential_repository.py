"""Integration tests for StudentCredentialRepository (Phase 6 T1).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``--run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Student, Tenant
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


async def _make_student(
    session: AsyncSession, tenant: Tenant, external_id: str
) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=external_id
    )


class TestCreateAndGet:
    async def test_create_then_get_by_student_id(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-cred-1")
        repo = StudentCredentialRepository(db_session)
        cred = await repo.create(
            student_id=student.id,
            tenant_id=seed_tenant.id,
            login="alice",
            password_hash="$argon2id$hash",
        )
        assert cred.is_active is True

        fetched = await repo.get_by_student_id(student.id)
        assert fetched is not None
        assert fetched.login == "alice"

    async def test_duplicate_login_per_tenant_raises(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        s1 = await _make_student(db_session, seed_tenant, "ext-a")
        s2 = await _make_student(db_session, seed_tenant, "ext-b")
        repo = StudentCredentialRepository(db_session)
        await repo.create(
            student_id=s1.id, tenant_id=seed_tenant.id, login="dup", password_hash="h"
        )
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await repo.create(
                    student_id=s2.id,
                    tenant_id=seed_tenant.id,
                    login="dup",
                    password_hash="h",
                )

    async def test_one_credential_per_student_raises(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-1to1")
        repo = StudentCredentialRepository(db_session)
        await repo.create(
            student_id=student.id,
            tenant_id=seed_tenant.id,
            login="login-1",
            password_hash="h",
        )
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await repo.create(
                    student_id=student.id,
                    tenant_id=seed_tenant.id,
                    login="login-2",
                    password_hash="h",
                )


class TestLoginLookup:
    async def test_get_active_returns_active(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-active")
        repo = StudentCredentialRepository(db_session)
        await repo.create(
            student_id=student.id,
            tenant_id=seed_tenant.id,
            login="bob",
            password_hash="h",
        )
        got = await repo.get_active_by_tenant_login(seed_tenant.id, "bob")
        assert got is not None
        assert got.student_id == student.id

    async def test_get_active_skips_revoked(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-revoked")
        repo = StudentCredentialRepository(db_session)
        await repo.create(
            student_id=student.id,
            tenant_id=seed_tenant.id,
            login="carol",
            password_hash="h",
        )
        await repo.set_active(student.id, is_active=False)
        got = await repo.get_active_by_tenant_login(seed_tenant.id, "carol")
        assert got is None

    async def test_get_active_unknown_login_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentCredentialRepository(db_session)
        got = await repo.get_active_by_tenant_login(seed_tenant.id, "ghost")
        assert got is None


class TestSetActive:
    async def test_set_active_toggles(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-toggle")
        repo = StudentCredentialRepository(db_session)
        await repo.create(
            student_id=student.id,
            tenant_id=seed_tenant.id,
            login="dave",
            password_hash="h",
        )
        assert await repo.set_active(student.id, is_active=False) is True
        revoked = await repo.get_by_student_id(student.id)
        assert revoked is not None
        assert revoked.is_active is False

        assert await repo.set_active(student.id, is_active=True) is True
        restored = await repo.get_by_student_id(student.id)
        assert restored is not None
        assert restored.is_active is True

    async def test_set_active_unknown_student_false(
        self, db_session: AsyncSession
    ) -> None:
        repo = StudentCredentialRepository(db_session)
        assert await repo.set_active(uuid.uuid4(), is_active=False) is False
