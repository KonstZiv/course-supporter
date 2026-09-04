"""Integration tests for CriteriaCacheService (sprint-mentor T4, D11).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db -v`` against this file.

Exercises the lazy read-through cache against a real DB with a fake
decomposer (no LLM):

* first call on a ready task → compute + persist + return.
* version match (same content_hash + task_type) → reuse, zero LLM.
* stale content_hash / changed task_type → recompute (release-old/insert-new).
* content guard: no ready DocumentSummary (or not a task) → None, no LLM.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.criteria_cache import CriteriaCacheService
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    TaskCriteria,
)
from course_supporter.storage.task_criteria_repository import TaskCriteriaRepository

pytestmark = pytest.mark.requires_db

_CRITERIA: list[dict[str, str]] = [
    {"statement": "Has a base case", "evidence": "explicit if-return"},
]


class _FakeDecomposer:
    """Records calls + returns canned criteria (no LLM)."""

    def __init__(self, criteria: list[dict[str, str]]) -> None:
        self.criteria = criteria
        self.calls = 0
        self.last: dict[str, object] | None = None

    async def decompose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
    ) -> list[dict[str, str]]:
        self.calls += 1
        self.last = {
            "task_title": task_title,
            "task_description": task_description,
            "task_text": task_text,
            "task_type": task_type,
            "language": language,
        }
        return self.criteria


async def _seed_ready_task(
    db_session: AsyncSession,
    doc: AuthoredDocument,
    *,
    task_type: str = "task",
    content: str = "Implement a recursive factorial.",
) -> DocumentSummary:
    """Make ``doc`` a ready task: set task_type, create a ready summary
    (which propagates content_hash up to the document) + one segment.
    """
    doc.task_type = task_type
    await db_session.flush()
    summary = await DocumentSummaryRepository(db_session).create(
        authored_document_id=doc.id,
        title="The Task",
        description="Task description.",
        main_concepts=[],
        secondary_concepts=[],
        content_char_count=len(content),
    )
    segment = DocumentSegment(
        document_summary_id=summary.id,
        course_root_id=summary.course_root_id,
        order=0,
        start_pos=0,
        end_pos=len(content),
        content=content,
    )
    db_session.add(segment)
    await db_session.flush()
    await db_session.refresh(doc)  # read the cascade-materialised content_hash
    return summary


class TestComputeOnMiss:
    async def test_first_call_computes_and_persists(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        await _seed_ready_task(db_session, seed_material_entry, content="task body")
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        row = await service.get_or_compute(seed_material_entry.id)

        assert decomposer.calls == 1
        assert row is not None
        assert row.criteria == _CRITERIA
        assert row.source_content_hash == seed_material_entry.content_hash
        assert row.source_task_type == "task"

    async def test_the_decomposer_is_given_a_language_name_not_a_code(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        # The source of the language is unchanged -- criteria are cached per
        # task and shared, so they follow the course, never one student's
        # request. Only the form handed to the model changes: a name it
        # recognises instead of the stored ``ukr``.
        seed_material_entry.language = "ukr"
        await _seed_ready_task(db_session, seed_material_entry, content="task body")
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        await service.get_or_compute(seed_material_entry.id)

        assert decomposer.last is not None
        assert decomposer.last["language"] == "Ukrainian"
        # The full segment text reached the decomposer.
        assert decomposer.last is not None
        assert decomposer.last["task_text"] == "task body"
        assert decomposer.last["task_type"] == "task"


class TestCacheHit:
    async def test_version_match_reuses_without_llm(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        await _seed_ready_task(db_session, seed_material_entry)
        # Pre-seed a matching active row.
        existing = await TaskCriteriaRepository(db_session).create(
            authored_document_id=seed_material_entry.id,
            criteria=[{"statement": "cached", "evidence": "stored"}],
            source_content_hash=seed_material_entry.content_hash or "",
            source_task_type="task",
        )
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        row = await service.get_or_compute(seed_material_entry.id)

        assert decomposer.calls == 0
        assert row is not None
        assert row.id == existing.id


class TestRecomputeOnStaleVersion:
    async def test_stale_content_hash_recomputes(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        await _seed_ready_task(db_session, seed_material_entry)
        repo = TaskCriteriaRepository(db_session)
        stale = await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=[{"statement": "old", "evidence": "v1"}],
            source_content_hash="stale" + "0" * 59,  # != current content_hash
            source_task_type="task",
        )
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        row = await service.get_or_compute(seed_material_entry.id)

        assert decomposer.calls == 1
        assert row is not None
        assert row.id != stale.id
        assert row.source_content_hash == seed_material_entry.content_hash
        # Old row soft-deleted; exactly one active.
        await db_session.refresh(stale)
        assert stale.deleted_at is not None
        active = await repo.get_active(seed_material_entry.id)
        assert active is not None
        assert active.id == row.id

    async def test_changed_task_type_recomputes(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        await _seed_ready_task(db_session, seed_material_entry, task_type="project")
        repo = TaskCriteriaRepository(db_session)
        await repo.create(
            authored_document_id=seed_material_entry.id,
            criteria=[{"statement": "old", "evidence": "v1"}],
            source_content_hash=seed_material_entry.content_hash or "",
            source_task_type="test",  # type changed since this was cached
        )
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        row = await service.get_or_compute(seed_material_entry.id)

        assert decomposer.calls == 1
        assert row is not None
        assert row.source_task_type == "project"


class TestContentGuard:
    async def test_not_a_task_returns_none(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        # task_type left NULL → not a task.
        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        assert await service.get_or_compute(seed_material_entry.id) is None
        assert decomposer.calls == 0

    async def test_no_ready_summary_returns_none(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        # A task with a content_hash but no ready DocumentSummary (pending).
        seed_material_entry.task_type = "task"
        seed_material_entry.content_hash = "f" * 64
        await db_session.flush()
        pending = DocumentSummary(
            authored_document_id=seed_material_entry.id,
            course_root_id=seed_root_node.id,
            title="t",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=0,
            status="pending",
        )
        db_session.add(pending)
        await db_session.flush()

        decomposer = _FakeDecomposer(_CRITERIA)
        service = CriteriaCacheService(db_session, decomposer)

        assert await service.get_or_compute(seed_material_entry.id) is None
        assert decomposer.calls == 0
        # No criteria row written.
        count = await db_session.execute(
            select(func.count())
            .select_from(TaskCriteria)
            .where(TaskCriteria.authored_document_id == seed_material_entry.id)
        )
        assert count.scalar_one() == 0
