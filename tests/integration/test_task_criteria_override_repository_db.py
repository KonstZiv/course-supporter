"""Integration tests for TaskCriteriaOverrideRepository (mentor-rebuild task 08, K1).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db`` against this file.

The author's layer of a criteria list (``TASK.md`` section 3.6): one live edit
per task, replaced whole, and every earlier state kept as a snapshot — a
replacement and a reset soft-delete, never delete.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import TextClause, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import AuthoredDocument, TaskCriteriaOverride
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)

pytestmark = pytest.mark.requires_db

_HASH = "a" * 64
_FIRST: list[dict[str, Any]] = [{"id": "c1", "text": "Defines a base case"}]
_SECOND: list[dict[str, Any]] = [{"id": "c1", "text": "Defines a base case first"}]
_WHEN = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


async def _replace(
    repo: TaskCriteriaOverrideRepository,
    document: AuthoredDocument,
    criteria: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> TaskCriteriaOverride:
    return await repo.replace(
        authored_document_id=document.id,
        source_content_hash=_HASH,
        source_task_type="task",
        criteria=criteria,
        now=now,
    )


async def _all(
    session: AsyncSession, document: AuthoredDocument
) -> list[TaskCriteriaOverride]:
    result = await session.execute(
        select(TaskCriteriaOverride)
        .where(TaskCriteriaOverride.authored_document_id == document.id)
        .order_by(TaskCriteriaOverride.created_at, TaskCriteriaOverride.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _count(session: AsyncSession, document: AuthoredDocument) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(TaskCriteriaOverride)
        .where(TaskCriteriaOverride.authored_document_id == document.id)
    )
    return result.scalar_one()


def _insert_live_edit_sql() -> TextClause:
    return text(
        "INSERT INTO task_criteria_overrides (id, authored_document_id, "
        "source_content_hash, source_task_type, criteria) "
        "VALUES (:id, :doc, :content, 'task', CAST(:criteria AS jsonb))"
    )


class TestReplace:
    async def test_replace_makes_the_edit_live(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaOverrideRepository(db_session)

        edit = await _replace(repo, seed_material_entry, _FIRST)

        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == edit.id
        assert live.criteria == _FIRST
        assert (live.source_content_hash, live.source_task_type) == (_HASH, "task")

    async def test_a_second_replace_keeps_the_first_as_a_snapshot(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaOverrideRepository(db_session)
        first = await _replace(repo, seed_material_entry, _FIRST)

        second = await _replace(repo, seed_material_entry, _SECOND, now=_WHEN)

        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == second.id
        assert live.criteria == _SECOND
        rows = {row.id: row for row in await _all(db_session, seed_material_entry)}
        assert set(rows) == {first.id, second.id}
        assert rows[first.id].deleted_at == _WHEN
        assert rows[first.id].criteria == _FIRST

    async def test_a_replace_that_meets_a_concurrent_edit_releases_it_and_wins(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another request's edit lands between this one's release and insert:
        the insert meets it, and the next attempt makes this edit the live one."""
        repo = TaskCriteriaOverrideRepository(db_session)
        release = repo._release_live
        calls = 0
        competitor = uuid.uuid4()

        async def release_then_competitor_lands(
            authored_document_id: uuid.UUID, *, now: datetime | None
        ) -> bool:
            nonlocal calls
            calls += 1
            released = await release(authored_document_id, now=now)
            if calls == 1:
                await db_session.execute(
                    _insert_live_edit_sql(),
                    {
                        "id": competitor,
                        "doc": authored_document_id,
                        "content": _HASH,
                        "criteria": json.dumps(_FIRST),
                    },
                )
            return released

        monkeypatch.setattr(repo, "_release_live", release_then_competitor_lands)

        edit = await _replace(repo, seed_material_entry, _SECOND)

        assert calls == 2
        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == edit.id
        rows = {row.id: row for row in await _all(db_session, seed_material_entry)}
        assert set(rows) == {competitor, edit.id}
        assert rows[competitor].deleted_at is not None

    async def test_the_database_holds_one_live_edit_per_task(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaOverrideRepository(db_session)
        await _replace(repo, seed_material_entry, _FIRST)

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                _insert_live_edit_sql(),
                {
                    "id": uuid.uuid4(),
                    "doc": seed_material_entry.id,
                    "content": _HASH,
                    "criteria": json.dumps(_SECOND),
                },
            )
        assert "uq_task_criteria_overrides_authored_document_id_active" in str(
            exc_info.value
        )

    async def test_the_database_refuses_an_empty_edit(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                _insert_live_edit_sql(),
                {
                    "id": uuid.uuid4(),
                    "doc": seed_material_entry.id,
                    "content": _HASH,
                    "criteria": "[]",
                },
            )
        assert "ck_task_criteria_overrides_criteria_present" in str(exc_info.value)


class TestReset:
    async def test_reset_soft_deletes_the_live_edit_and_keeps_it(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaOverrideRepository(db_session)
        edit = await _replace(repo, seed_material_entry, _FIRST)

        assert await repo.reset(seed_material_entry.id, now=_WHEN) is True

        assert await repo.get_live(seed_material_entry.id) is None
        rows = await _all(db_session, seed_material_entry)
        assert [row.id for row in rows] == [edit.id]
        assert rows[0].deleted_at == _WHEN
        assert rows[0].criteria == _FIRST

    async def test_reset_without_a_live_edit_returns_false(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaOverrideRepository(db_session)

        assert await repo.reset(seed_material_entry.id) is False
        assert await _count(db_session, seed_material_entry) == 0
