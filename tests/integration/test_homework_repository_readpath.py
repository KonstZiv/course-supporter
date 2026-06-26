"""Integration tests for HomeworkRepository read-path queries (Phase 6 T4a c2).

Pins the Q1 invariant: ``list_active_submissions_in_course`` is course-scoped,
ownership-scoped, and EXCLUDES soft-deleted submissions (curated read-path) —
distinct from the soft-delete-agnostic, review-graph-shared ``get_for_student``,
which is left untouched. Requires ``docker compose up -d`` (PostgreSQL);
run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


async def _student(session: AsyncSession, tenant: Tenant, external_id: str) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=external_id
    )


async def _submit(
    repo: HomeworkRepository,
    *,
    tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
    filename: str,
) -> HomeworkSubmission:
    return await repo.create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url=f"s3://bucket/{filename}",
        file_type="text/plain",
        original_filename=filename,
        delivery_mode="in_app",
    )


class TestListActiveSubmissionsInCourse:
    async def test_returns_active_excludes_soft_deleted(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Active submissions returned; a soft-deleted one is filtered out (Q7)."""
        student = await _student(db_session, seed_tenant, "ext-active-keep")
        repo = HomeworkRepository(db_session)
        live = await _submit(
            repo,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            filename="live.py",
        )
        gone = await _submit(
            repo,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            filename="gone.py",
        )
        gone.deleted_at = datetime.now(UTC)
        await db_session.flush()

        rows = await repo.list_active_submissions_in_course(
            student.id, seed_root_node.id
        )
        assert [r.id for r in rows] == [live.id]

    async def test_course_scoped(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A different course root → empty (course-scoped on course_node_id)."""
        student = await _student(db_session, seed_tenant, "ext-active-scope")
        repo = HomeworkRepository(db_session)
        await _submit(
            repo,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            filename="here.py",
        )
        rows = await repo.list_active_submissions_in_course(student.id, uuid.uuid4())
        assert rows == []

    async def test_empty_for_no_submissions(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """No submissions → empty list."""
        student = await _student(db_session, seed_tenant, "ext-active-empty")
        rows = await HomeworkRepository(db_session).list_active_submissions_in_course(
            student.id, seed_root_node.id
        )
        assert rows == []
