"""Repository for the author's edits of criteria lists (mentor-rebuild task 08).

Pure storage for the author's layer of a task's criteria list: one live edit
per task, replaced whole; a replacement and a reset soft-delete the previous
edit, so every earlier state stays as a snapshot (``TASK.md`` section 3.6).
Whether an edit is in force — its version keys against the task's current
content hash and type — is the criteria-list service's decision, not this
class's.

The caller owns the transaction: every method flushes and none commits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import TaskCriteriaOverride

# Each attempt releases the live edit it can see and inserts; an attempt that
# meets a concurrent insert starts over. An author's own requests are the only
# writers, so three covers any realistic interleaving.
_REPLACE_ATTEMPTS: Final[int] = 3


class TaskCriteriaOverrideRepository:
    """Rows of ``task_criteria_overrides``: the live edit and its snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_live(
        self, authored_document_id: uuid.UUID
    ) -> TaskCriteriaOverride | None:
        """Return the task's live edit, or None when the author has none."""
        stmt = (
            select(TaskCriteriaOverride)
            .where(
                TaskCriteriaOverride.authored_document_id == authored_document_id,
                TaskCriteriaOverride.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def replace(
        self,
        *,
        authored_document_id: uuid.UUID,
        source_content_hash: str,
        source_task_type: str,
        criteria: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> TaskCriteriaOverride:
        """Make this edit the live one; the previous live edit becomes a snapshot.

        Of two replacements that arrive together, the last to write wins. The
        insert carries ``ON CONFLICT DO NOTHING``, so meeting a concurrent
        insert does not poison the caller's transaction; the next attempt
        releases that edit — now visible — and inserts again.

        Args:
            authored_document_id: The task the edit belongs to.
            source_content_hash: The task version's ``content_hash``.
            source_task_type: The task version's ``task_type``.
            criteria: The author's whole list, in the task-08 criterion form.
            now: Override for the snapshot's soft-delete timestamp (testing).

        Returns:
            The new live edit.

        Raises:
            RuntimeError: No attempt could insert — concurrent replacements
                kept winning.
        """
        for _ in range(_REPLACE_ATTEMPTS):
            await self._release_live(authored_document_id, now=now)
            stmt = (
                pg_insert(TaskCriteriaOverride)
                .values(
                    authored_document_id=authored_document_id,
                    source_content_hash=source_content_hash,
                    source_task_type=source_task_type,
                    criteria=criteria,
                )
                .on_conflict_do_nothing()
                .returning(TaskCriteriaOverride)
                .execution_options(populate_existing=True)
            )
            created = (await self._session.execute(stmt)).scalar_one_or_none()
            if created is not None:
                await self._session.flush()
                return created

        msg = (
            f"Could not store the author's criteria for task "
            f"{authored_document_id} after {_REPLACE_ATTEMPTS} attempts."
        )
        raise RuntimeError(msg)

    async def reset(
        self, authored_document_id: uuid.UUID, *, now: datetime | None = None
    ) -> bool:
        """Soft-delete the live edit; the row stays as a snapshot.

        Returns:
            True if there was a live edit to reset.
        """
        return await self._release_live(authored_document_id, now=now)

    async def _release_live(
        self, authored_document_id: uuid.UUID, *, now: datetime | None
    ) -> bool:
        stmt = (
            update(TaskCriteriaOverride)
            .where(
                TaskCriteriaOverride.authored_document_id == authored_document_id,
                TaskCriteriaOverride.deleted_at.is_(None),
            )
            .values(deleted_at=now or datetime.now(UTC))
            .returning(TaskCriteriaOverride.id)
            .execution_options(synchronize_session=False)
        )
        released = (await self._session.execute(stmt)).scalars().all()
        await self._session.flush()
        return bool(released)
