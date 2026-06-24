"""Integration tests for TaskCriteriaRepository (sprint-mentor T4).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db -v`` against this file.

Covers the storage layer of the D11 criteria cache (vision §1386):

* ``create`` persists a row with the version keys + criteria payload.
* ``get_active`` round-trips and ignores soft-deleted rows.
* ``release_active`` soft-deletes the active row (no-op when none).
* release + create cycle (re-version) leaves exactly one active row
  with soft-deleted history — no partial-active-unique violation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import AuthoredDocument, CourseNode, TaskCriteria
from course_supporter.storage.task_criteria_repository import TaskCriteriaRepository

pytestmark = pytest.mark.requires_db

_CRITERIA: list[dict[str, str]] = [
    {"statement": "Defines a recursive base case", "evidence": "explicit if-return"},
    {"statement": "Recurses toward the base case", "evidence": "argument shrinks"},
]


class TestCreate:
    async def test_create_persists_row_with_version_keys(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = TaskCriteriaRepository(db_session)

        row = await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=_CRITERIA,
            source_content_hash="a" * 64,
            source_task_type="task",
        )

        assert row.id is not None
        assert row.authored_document_id == seed_material_entry.id
        assert row.criteria == _CRITERIA
        assert row.source_content_hash == "a" * 64
        assert row.source_task_type == "task"
        assert row.deleted_at is None


class TestGetActive:
    async def test_get_active_returns_inserted_row(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = TaskCriteriaRepository(db_session)
        created = await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=_CRITERIA,
            source_content_hash="b" * 64,
            source_task_type="project",
        )

        fetched = await repo.get_active(seed_material_entry.id)

        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_active_returns_none_when_absent(
        self,
        db_session: AsyncSession,
    ) -> None:
        repo = TaskCriteriaRepository(db_session)
        assert await repo.get_active(uuid.uuid4()) is None

    async def test_get_active_ignores_soft_deleted(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = TaskCriteriaRepository(db_session)
        await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=_CRITERIA,
            source_content_hash="c" * 64,
            source_task_type="task",
        )

        await repo.release_active(seed_material_entry.id)

        assert await repo.get_active(seed_material_entry.id) is None


class TestReleaseActive:
    async def test_release_active_is_noop_without_active_row(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = TaskCriteriaRepository(db_session)
        # Must not raise when there is nothing to release.
        await repo.release_active(seed_material_entry.id)
        assert await repo.get_active(seed_material_entry.id) is None

    async def test_reversion_keeps_one_active_with_history(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """release + create (re-version) → exactly one active, no unique violation."""
        repo = TaskCriteriaRepository(db_session)
        old = await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=_CRITERIA,
            source_content_hash="d" * 64,
            source_task_type="task",
        )

        await repo.release_active(seed_material_entry.id)
        new = await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=[{"statement": "fresh", "evidence": "v2"}],
            source_content_hash="e" * 64,
            source_task_type="task",
        )

        # Exactly one active row, and it is the new one.
        active = await repo.get_active(seed_material_entry.id)
        assert active is not None
        assert active.id == new.id
        assert active.source_content_hash == "e" * 64

        # Both rows persist; the old one is soft-deleted (audit history).
        total = await db_session.execute(
            select(func.count())
            .select_from(TaskCriteria)
            .where(TaskCriteria.authored_document_id == seed_material_entry.id)
        )
        assert total.scalar_one() == 2
        await db_session.refresh(old)
        assert old.deleted_at is not None
