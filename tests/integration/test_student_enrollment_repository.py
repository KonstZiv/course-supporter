"""Integration tests for StudentEnrollmentRepository (Phase 6 T1).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``--run-db``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import CourseNode, Student, Tenant
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


async def _make_student(
    session: AsyncSession, tenant: Tenant, external_id: str
) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=external_id
    )


class TestBind:
    async def test_bind_then_list(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-bind")
        repo = StudentEnrollmentRepository(db_session)
        enrollment = await repo.bind(
            student_id=student.id, course_node_id=seed_root_node.id
        )
        assert enrollment.id is not None

        enrollments = await repo.list_for_student(student.id)
        assert len(enrollments) == 1
        assert enrollments[0].course_node_id == seed_root_node.id

    async def test_duplicate_bind_raises(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-dupbind")
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=seed_root_node.id)
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await repo.bind(student_id=student.id, course_node_id=seed_root_node.id)


class TestUnbind:
    async def test_unbind_deletes_row(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-unbind")
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=seed_root_node.id)

        deleted = await repo.unbind(
            student_id=student.id, course_node_id=seed_root_node.id
        )
        assert deleted is True
        assert await repo.list_for_student(student.id) == []

    async def test_unbind_unbound_returns_false(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        student = await _make_student(db_session, seed_tenant, "ext-nounbind")
        repo = StudentEnrollmentRepository(db_session)
        deleted = await repo.unbind(
            student_id=student.id, course_node_id=seed_root_node.id
        )
        assert deleted is False
