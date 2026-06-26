"""Integration tests for StudentEnrollmentRepository (Phase 6 T1).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``--run-db``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import CourseNode, Student, Tenant
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.course_node_factory import make_root_course_node

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


class TestIsEnrolled:
    async def test_enrolled_true(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """is_enrolled is True after a bind (Phase 6 T2 submission gate)."""
        student = await _make_student(db_session, seed_tenant, "ext-enr-true")
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=seed_root_node.id)
        assert await repo.is_enrolled(student.id, seed_root_node.id) is True

    async def test_not_enrolled_false(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """is_enrolled is False with no bind."""
        student = await _make_student(db_session, seed_tenant, "ext-enr-false")
        repo = StudentEnrollmentRepository(db_session)
        assert await repo.is_enrolled(student.id, seed_root_node.id) is False

    async def test_unbind_makes_not_enrolled(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """is_enrolled flips back to False after unbind (revoke course access)."""
        student = await _make_student(db_session, seed_tenant, "ext-enr-unbind")
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=seed_root_node.id)
        await repo.unbind(student_id=student.id, course_node_id=seed_root_node.id)
        assert await repo.is_enrolled(student.id, seed_root_node.id) is False


class TestListEnrolledCourses:
    async def test_returns_enrolled_roots(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
    ) -> None:
        """list_enrolled_courses JOINs to the enrolled roots (id + title).

        Asserted as a set: both enrollments are created in the one savepoint
        transaction, so their ``created_at`` (PostgreSQL ``transaction_timestamp``)
        is identical and the oldest-first order between them is not deterministic
        — the same property the pre-existing ``list_for_student`` carries. The
        contract under test is membership + the JOIN, not the intra-transaction
        tiebreak.
        """
        student = await _make_student(db_session, seed_tenant, "ext-list-courses")
        first = make_root_course_node(
            tenant_id=seed_tenant.id, title="First Course", order=0
        )
        second = make_root_course_node(
            tenant_id=seed_tenant.id, title="Second Course", order=1
        )
        db_session.add_all([first, second])
        await db_session.flush()
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=first.id)
        await repo.bind(student_id=student.id, course_node_id=second.id)

        courses = await repo.list_enrolled_courses(student.id)
        assert {(c.id, c.title) for c in courses} == {
            (first.id, "First Course"),
            (second.id, "Second Course"),
        }

    async def test_excludes_soft_deleted_root(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
    ) -> None:
        """A soft-deleted enrolled root is filtered out (deleted_at IS NULL)."""
        student = await _make_student(db_session, seed_tenant, "ext-list-softdel")
        active = make_root_course_node(tenant_id=seed_tenant.id, title="Active")
        gone = make_root_course_node(tenant_id=seed_tenant.id, title="Gone")
        db_session.add_all([active, gone])
        await db_session.flush()
        repo = StudentEnrollmentRepository(db_session)
        await repo.bind(student_id=student.id, course_node_id=active.id)
        await repo.bind(student_id=student.id, course_node_id=gone.id)
        gone.deleted_at = datetime.now(UTC)
        await db_session.flush()

        courses = await repo.list_enrolled_courses(student.id)
        assert [c.id for c in courses] == [active.id]

    async def test_empty_for_no_enrollments(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
    ) -> None:
        """No enrollments → empty list."""
        student = await _make_student(db_session, seed_tenant, "ext-list-empty")
        repo = StudentEnrollmentRepository(db_session)
        assert await repo.list_enrolled_courses(student.id) == []
