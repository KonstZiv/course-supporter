"""Integration tests for TaskCriteriaListRepository (mentor-rebuild task 08, K1).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db`` against this file.

The storage rules of the claim protocol (``TASK.md`` section 9, decision 2):

* one live row per task — a claim or a ready list — held by the database;
* every transition is conditional on what its caller saw, so a claimer that
  lost its claim can neither finish nor fail it;
* a failed composition is history, never live, and frees the task;
* a soft-deleted task takes its list and its author's edit with it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.storage.cascade import CascadeDeleteService, build_cascade_map
from course_supporter.storage.orm import (
    AuthoredDocument,
    TaskCriteriaList,
    TaskCriteriaOverride,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)

pytestmark = pytest.mark.requires_db

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_CRITERIA: list[dict[str, Any]] = [{"id": "c1", "text": "Defines a base case"}]
_LATER = timedelta(minutes=30)


async def _claim(
    repo: TaskCriteriaListRepository,
    document: AuthoredDocument,
    *,
    content_hash: str = _HASH_A,
) -> TaskCriteriaList:
    row = await repo.claim(
        authored_document_id=document.id,
        source_content_hash=content_hash,
        source_task_type="task",
        form_version=2,
    )
    assert row is not None
    return row


async def _mark_ready(
    repo: TaskCriteriaListRepository, list_id: uuid.UUID, claimed_at: datetime
) -> bool:
    return await repo.mark_ready(
        list_id,
        claimed_at=claimed_at,
        criteria=_CRITERIA,
        contradictions=[],
        concepts_in_input=True,
        dropped_concept_count=0,
        input_fingerprint="f" * 64,
    )


async def _rows(session: AsyncSession, document: AuthoredDocument) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(TaskCriteriaList)
        .where(TaskCriteriaList.authored_document_id == document.id)
    )
    return result.scalar_one()


class TestClaim:
    async def test_a_claim_is_a_pending_live_row(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)

        row = await _claim(repo, seed_material_entry)

        assert row.state == CriteriaListState.PENDING
        assert row.claimed_at is not None
        assert (row.source_content_hash, row.source_task_type) == (_HASH_A, "task")
        assert row.form_version == 2
        assert row.criteria is None
        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == row.id

    async def test_a_task_with_a_live_row_cannot_be_claimed_again(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        """A second claim — even for another version — meets the live row."""
        repo = TaskCriteriaListRepository(db_session)
        await _claim(repo, seed_material_entry)

        second = await repo.claim(
            authored_document_id=seed_material_entry.id,
            source_content_hash=_HASH_B,
            source_task_type="task",
            form_version=2,
        )

        assert second is None
        assert await _rows(db_session, seed_material_entry) == 1

    async def test_the_database_holds_one_live_row_per_task(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        await _claim(repo, seed_material_entry)

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text(
                    "INSERT INTO task_criteria_lists (id, authored_document_id, "
                    "source_content_hash, source_task_type, form_version) "
                    "VALUES (:id, :doc, :content, 'task', 2)"
                ),
                {"id": uuid.uuid4(), "doc": seed_material_entry.id, "content": _HASH_B},
            )
        assert "uq_task_criteria_lists_authored_document_id_active" in str(
            exc_info.value
        )


class TestRelease:
    async def test_release_frees_the_task_and_keeps_history(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        old = await _claim(repo, seed_material_entry)

        assert await repo.release(old.id) is True
        assert await repo.get_live(seed_material_entry.id) is None
        new = await _claim(repo, seed_material_entry, content_hash=_HASH_B)

        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == new.id
        assert await _rows(db_session, seed_material_entry) == 2
        released = await repo.get_by_id(old.id)
        assert released is not None
        assert released.deleted_at is not None

    async def test_a_second_release_of_one_row_leaves_the_new_claim_alone(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        """Two submissions that found the same stale list: the loser's release
        of that list must not reach the winner's fresh claim."""
        repo = TaskCriteriaListRepository(db_session)
        stale = await _claim(repo, seed_material_entry)
        assert await repo.release(stale.id) is True
        winner = await _claim(repo, seed_material_entry, content_hash=_HASH_B)

        assert await repo.release(stale.id) is False

        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.id == winner.id


class TestTakeOver:
    async def test_take_over_renews_the_claim_the_caller_saw(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        seen = row.claimed_at

        taken = await repo.take_over(row.id, seen_claimed_at=seen, now=seen + _LATER)

        assert taken == seen + _LATER
        reread = await repo.get_by_id(row.id)
        assert reread is not None
        assert reread.claimed_at == seen + _LATER
        assert reread.state == CriteriaListState.PENDING

    async def test_of_two_takeovers_of_one_seen_claim_only_the_first_wins(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        seen = row.claimed_at

        first = await repo.take_over(row.id, seen_claimed_at=seen, now=seen + _LATER)
        second = await repo.take_over(
            row.id, seen_claimed_at=seen, now=seen + 2 * _LATER
        )

        assert first == seen + _LATER
        assert second is None

    async def test_a_ready_list_cannot_be_taken_over(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        seen = row.claimed_at
        assert await _mark_ready(repo, row.id, seen) is True

        assert (
            await repo.take_over(row.id, seen_claimed_at=seen, now=seen + _LATER)
            is None
        )


class TestMarkReady:
    async def test_mark_ready_stores_the_list(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)

        assert await _mark_ready(repo, row.id, row.claimed_at) is True

        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.state == CriteriaListState.READY
        assert live.criteria == _CRITERIA
        assert live.contradictions == []
        assert live.concepts_in_input is True
        assert live.dropped_concept_count == 0
        assert live.input_fingerprint == "f" * 64

    async def test_a_claimer_whose_claim_was_taken_over_cannot_finish(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        lost = row.claimed_at
        taken = await repo.take_over(row.id, seen_claimed_at=lost, now=lost + _LATER)
        assert taken is not None

        assert await _mark_ready(repo, row.id, lost) is False
        still = await repo.get_by_id(row.id)
        assert still is not None
        assert still.state == CriteriaListState.PENDING
        assert still.criteria is None

        assert await _mark_ready(repo, row.id, taken) is True

    async def test_the_database_refuses_an_incomplete_ready_row(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text("UPDATE task_criteria_lists SET state = 'ready' WHERE id = :id"),
                {"id": row.id},
            )
        assert "ck_task_criteria_lists_ready_complete" in str(exc_info.value)


class TestMarkFailed:
    async def test_a_failed_composition_becomes_history_and_frees_the_task(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        when = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

        failed = await repo.mark_failed(
            row.id,
            claimed_at=row.claimed_at,
            failure_reason="ladder exhausted",
            now=when,
        )

        assert failed is True
        assert await repo.get_live(seed_material_entry.id) is None
        history = await repo.get_by_id(row.id)
        assert history is not None
        assert history.state == CriteriaListState.FAILED
        assert history.failure_reason == "ladder exhausted"
        assert history.deleted_at == when
        assert await _claim(repo, seed_material_entry) is not None

    async def test_a_claimer_whose_claim_was_taken_over_cannot_fail_it(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)
        lost = row.claimed_at
        assert (
            await repo.take_over(row.id, seen_claimed_at=lost, now=lost + _LATER)
            is not None
        )

        assert (
            await repo.mark_failed(row.id, claimed_at=lost, failure_reason="gave up")
            is False
        )
        live = await repo.get_live(seed_material_entry.id)
        assert live is not None
        assert live.state == CriteriaListState.PENDING

    async def test_the_database_refuses_a_live_failed_row(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text("UPDATE task_criteria_lists SET state = 'failed' WHERE id = :id"),
                {"id": row.id},
            )
        assert "ck_task_criteria_lists_failed_is_history" in str(exc_info.value)


class TestStateVocabulary:
    async def test_the_database_refuses_an_unknown_state(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        repo = TaskCriteriaListRepository(db_session)
        row = await _claim(repo, seed_material_entry)

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text(
                    "UPDATE task_criteria_lists SET state = 'composing' WHERE id = :id"
                ),
                {"id": row.id},
            )
        assert "ck_task_criteria_lists_state" in str(exc_info.value)


class TestCascade:
    async def test_a_soft_deleted_task_takes_its_list_and_its_edit(
        self, db_session: AsyncSession, seed_material_entry: AuthoredDocument
    ) -> None:
        lists = TaskCriteriaListRepository(db_session)
        edits = TaskCriteriaOverrideRepository(db_session)
        row = await _claim(lists, seed_material_entry)
        assert await _mark_ready(lists, row.id, row.claimed_at) is True
        edit = await edits.replace(
            authored_document_id=seed_material_entry.id,
            source_content_hash=_HASH_A,
            source_task_type="task",
            criteria=_CRITERIA,
        )

        await CascadeDeleteService(db_session).soft_delete_with_cascade(
            seed_material_entry, build_cascade_map(AuthoredDocument)
        )

        assert await lists.get_live(seed_material_entry.id) is None
        assert await edits.get_live(seed_material_entry.id) is None
        kept_list = await lists.get_by_id(row.id)
        assert kept_list is not None
        assert kept_list.deleted_at is not None
        kept_edit = await db_session.get(
            TaskCriteriaOverride, edit.id, populate_existing=True
        )
        assert kept_edit is not None
        assert kept_edit.deleted_at is not None
