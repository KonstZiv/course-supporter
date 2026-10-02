"""Repository for the criteria lists of task versions (mentor-rebuild task 08).

Pure storage for the machine layer of a task's criteria list and the primitives
of its claim protocol (``TASK.md`` section 9, decision 2). Deciding when to
claim, wait, take over and compose is the criteria-list service's job; this
class reads rows and performs single-statement transitions.

Every transition is conditional on what its caller last saw — the row id, the
state, ``claimed_at`` — so an outcome a race can invalidate is settled by the
database rather than by the caller's memory: a transition that finds the row
changed does nothing and returns ``None`` or ``False``.

The caller owns the transactions: every method flushes and none commits. The
claim protocol commits its claim, its final write and a takeover in short
transactions of their own, so that a model call never holds a lock or a
connection.

Updates bypass the session's identity map (``synchronize_session=False``);
the readers load with ``populate_existing``, so a row read after a transition
shows the database, not a stale object.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.storage.orm import TaskCriteriaList


class TaskCriteriaListRepository:
    """Rows of ``task_criteria_lists`` and the transitions of their lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_live(
        self, authored_document_id: uuid.UUID
    ) -> TaskCriteriaList | None:
        """Return the live row of a task — a claim or a ready list — or None.

        At most one row can be live (the partial unique index
        ``uq_task_criteria_lists_authored_document_id_active``); a failed
        composition is never live.
        """
        stmt = (
            select(TaskCriteriaList)
            .where(
                TaskCriteriaList.authored_document_id == authored_document_id,
                TaskCriteriaList.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, list_id: uuid.UUID) -> TaskCriteriaList | None:
        """Return one row whatever its state — what a waiting submission re-reads."""
        stmt = (
            select(TaskCriteriaList)
            .where(TaskCriteriaList.id == list_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_for_version(
        self,
        authored_document_id: uuid.UUID,
        *,
        source_content_hash: str,
        source_task_type: str,
    ) -> TaskCriteriaList | None:
        """Return a task version's most recent row, history included (task 09b).

        What the author's reading asks while no list is in force: is one being
        composed for this version, or did the last attempt fail — and why. A
        failed attempt is history (never live), so :meth:`get_live` cannot say.
        """
        stmt = (
            select(TaskCriteriaList)
            .where(
                TaskCriteriaList.authored_document_id == authored_document_id,
                TaskCriteriaList.source_content_hash == source_content_hash,
                TaskCriteriaList.source_task_type == source_task_type,
            )
            .order_by(TaskCriteriaList.created_at.desc(), TaskCriteriaList.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def claim(
        self,
        *,
        authored_document_id: uuid.UUID,
        source_content_hash: str,
        source_task_type: str,
        form_version: int,
    ) -> TaskCriteriaList | None:
        """Insert a ``pending`` claim for one task version.

        Returns None when the task already has a live row — another
        submission's claim or a ready list. The insert carries
        ``ON CONFLICT DO NOTHING``, so losing that race returns None instead of
        raising ``IntegrityError``, which would poison the caller's
        transaction. A stale live row must be released first
        (:meth:`release`).

        Args:
            authored_document_id: The task the list belongs to.
            source_content_hash: Content-axis version key (the document's
                ``content_hash`` at claim time).
            source_task_type: Type-axis version key (the document's
                ``task_type`` at claim time).
            form_version: The criterion form the list will be composed in.

        Returns:
            The new ``pending`` row, or None if the task holds a live row.
        """
        stmt = (
            pg_insert(TaskCriteriaList)
            .values(
                authored_document_id=authored_document_id,
                source_content_hash=source_content_hash,
                source_task_type=source_task_type,
                form_version=form_version,
                state=CriteriaListState.PENDING.value,
            )
            .on_conflict_do_nothing()
            .returning(TaskCriteriaList)
            .execution_options(populate_existing=True)
        )
        created = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return created

    async def release(self, list_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        """Soft-delete this row if it is still live; True when it was.

        Conditional on the row id, not on "whatever is live for the task": two
        submissions that both found the same stale list must not release each
        other's fresh claim. The loser finds the row already released, gets
        False, and its :meth:`claim` then meets the winner's claim.

        Args:
            list_id: The row to release — the list the caller found stale.
            now: Override for the soft-delete timestamp (testing).
        """
        stmt = (
            update(TaskCriteriaList)
            .where(
                TaskCriteriaList.id == list_id,
                TaskCriteriaList.deleted_at.is_(None),
            )
            .values(deleted_at=now or datetime.now(UTC))
            .returning(TaskCriteriaList.id)
            .execution_options(synchronize_session=False)
        )
        released = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return released is not None

    async def take_over(
        self,
        list_id: uuid.UUID,
        *,
        seen_claimed_at: datetime,
        now: datetime | None = None,
    ) -> datetime | None:
        """Take over an abandoned claim, unless somebody else has since.

        Renews ``claimed_at`` only while it still equals the value the caller
        saw when it judged the claim abandoned, so of two submissions that
        judged the same claim abandoned exactly one takes it over. Whether a
        claim is abandoned — how long its claimer has been silent — is the
        service's judgement, not this method's.

        Args:
            list_id: The ``pending`` row to take over.
            seen_claimed_at: ``claimed_at`` as the caller read it.
            now: The new claim time; the database's ``now()`` when omitted.

        Returns:
            The new ``claimed_at`` — the caller's claim from now on — or None
            if the row is no longer that pending claim.
        """
        return await self._renew_claimed_at(list_id, held=seen_claimed_at, now=now)

    async def renew(
        self,
        list_id: uuid.UUID,
        *,
        claimed_at: datetime,
        now: datetime | None = None,
    ) -> datetime | None:
        """Keep a live claim alive — the claimer's heartbeat.

        The same conditional update as :meth:`take_over`, for the claimer's own
        claim: it lands only while the row still holds the ``claimed_at`` the
        claimer wrote last. None means the claim was taken over in between —
        the claimer has lost it and stops beating, and its final write is
        refused by :meth:`mark_ready` and :meth:`mark_failed`.

        Args:
            list_id: The claimer's ``pending`` row.
            claimed_at: The claim time the claimer holds.
            now: The renewed claim time; the database's ``now()`` when omitted.

        Returns:
            The renewed ``claimed_at``, or None if the claim is no longer the
            caller's.
        """
        return await self._renew_claimed_at(list_id, held=claimed_at, now=now)

    async def _renew_claimed_at(
        self, list_id: uuid.UUID, *, held: datetime, now: datetime | None
    ) -> datetime | None:
        stmt = (
            update(TaskCriteriaList)
            .where(
                TaskCriteriaList.id == list_id,
                TaskCriteriaList.state == CriteriaListState.PENDING.value,
                TaskCriteriaList.deleted_at.is_(None),
                TaskCriteriaList.claimed_at == held,
            )
            .values(claimed_at=now if now is not None else func.now())
            .returning(TaskCriteriaList.claimed_at)
            .execution_options(synchronize_session=False)
        )
        claimed_at = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return claimed_at

    async def mark_ready(
        self,
        list_id: uuid.UUID,
        *,
        claimed_at: datetime,
        criteria: list[dict[str, Any]],
        contradictions: list[str],
        concepts_in_input: bool,
        dropped_concept_count: int,
        input_fingerprint: str,
    ) -> bool:
        """Store the composed list, if the claim is still the caller's.

        A claimer whose claim was taken over while it was calling the model
        gets False and writes nothing: the list belongs to whoever holds the
        claim now.

        Args:
            list_id: The caller's ``pending`` row.
            claimed_at: The claim time the caller holds.
            criteria: The criteria in the task-08 form.
            contradictions: Task-versus-node contradictions for the author.
            concepts_in_input: Whether a node or root summary was available.
            dropped_concept_count: Invented concepts dropped by code.
            input_fingerprint: SHA-256 of the input context (diagnostics).

        Returns:
            True if the row is now ``ready``.
        """
        stmt = (
            update(TaskCriteriaList)
            .where(
                TaskCriteriaList.id == list_id,
                TaskCriteriaList.state == CriteriaListState.PENDING.value,
                TaskCriteriaList.deleted_at.is_(None),
                TaskCriteriaList.claimed_at == claimed_at,
            )
            .values(
                state=CriteriaListState.READY.value,
                criteria=criteria,
                contradictions=contradictions,
                concepts_in_input=concepts_in_input,
                dropped_concept_count=dropped_concept_count,
                input_fingerprint=input_fingerprint,
                failure_reason=None,
            )
            .returning(TaskCriteriaList.id)
            .execution_options(synchronize_session=False)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return marked is not None

    async def mark_failed(
        self,
        list_id: uuid.UUID,
        *,
        claimed_at: datetime,
        failure_reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Record that the composition gave up and make the row history.

        ``failed`` and the soft delete land in one update: a failed row is
        never live (``ck_task_criteria_lists_failed_is_history``), so it frees
        the task for the next submission's claim. Conditional on the claim, as
        :meth:`mark_ready` is.

        Args:
            list_id: The caller's ``pending`` row.
            claimed_at: The claim time the caller holds.
            failure_reason: Why the composition gave up.
            now: Override for the soft-delete timestamp (testing).

        Returns:
            True if the row is now ``failed`` history.
        """
        stmt = (
            update(TaskCriteriaList)
            .where(
                TaskCriteriaList.id == list_id,
                TaskCriteriaList.state == CriteriaListState.PENDING.value,
                TaskCriteriaList.deleted_at.is_(None),
                TaskCriteriaList.claimed_at == claimed_at,
            )
            .values(
                state=CriteriaListState.FAILED.value,
                failure_reason=failure_reason,
                deleted_at=now or datetime.now(UTC),
            )
            .returning(TaskCriteriaList.id)
            .execution_options(synchronize_session=False)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return marked is not None
