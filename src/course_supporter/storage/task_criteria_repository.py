"""Repository for the TaskCriteria cache (vision §1386, D11).

Pure storage for the cached task→criteria decomposition. The lazy
read-through compute (content guard, version-match, LLM decomposition)
lives in :class:`course_supporter.homework.criteria_cache.CriteriaCacheService`;
this class only persists and reads rows.

One ACTIVE row per task is enforced by the partial-active unique index
``uq_task_criteria_authored_document_id_active``. A re-version calls
:meth:`TaskCriteriaRepository.release_active` before
:meth:`TaskCriteriaRepository.create`, mirroring the DocumentSummary
reprocess discipline (Task 3.2.6 Finding 2): soft-deleted history
coexists with the single active row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import TaskCriteria

logger = structlog.get_logger()


class TaskCriteriaRepository:
    """CRUD for cached task criteria rows (one active per task version)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, authored_document_id: uuid.UUID) -> TaskCriteria | None:
        """Return the single active (non-soft-deleted) row, or None.

        Args:
            authored_document_id: The task the criteria belong to.
        """
        stmt = select(TaskCriteria).where(
            TaskCriteria.authored_document_id == authored_document_id,
            TaskCriteria.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        authored_document_id: uuid.UUID,
        criteria: list[dict[str, str]],
        source_content_hash: str,
        source_task_type: str,
    ) -> TaskCriteria:
        """Insert a fresh criteria row for one task version.

        The caller must have released any prior active row first
        (:meth:`release_active`); the partial-active unique would
        otherwise reject a second active row for the same document.

        Args:
            authored_document_id: FK to the task AuthoredDocument.
            criteria: The decomposition — list of {statement, evidence}.
            source_content_hash: Content-axis version key (the document's
                ``content_hash`` at compute time).
            source_task_type: Type-axis version key (the document's
                ``task_type`` at compute time).

        Returns:
            The newly inserted TaskCriteria row.
        """
        row = TaskCriteria(
            authored_document_id=authored_document_id,
            criteria=criteria,
            source_content_hash=source_content_hash,
            source_task_type=source_task_type,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def release_active(
        self,
        authored_document_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        """Soft-delete the active row (if any) for this document.

        Executed as an explicit UPDATE (not ORM-object mutation) so the
        soft-delete lands in the transaction BEFORE a subsequent
        :meth:`create` INSERT — preventing two active rows from briefly
        coexisting and tripping the partial-active unique (the same
        flush-ordering hazard handled for DocumentSummary, Task 3.2.6).
        No active row is a no-op (first-time compute).

        Args:
            authored_document_id: The task whose active criteria to release.
            now: Override for the soft-delete timestamp (testing).
        """
        now = now or datetime.now(UTC)
        stmt = (
            update(TaskCriteria)
            .where(
                TaskCriteria.authored_document_id == authored_document_id,
                TaskCriteria.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        await self._session.execute(stmt)
        await self._session.flush()
