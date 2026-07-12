"""Lazy read-through cache for the task→criteria decomposition (D11).

Vision §1386: the Mentor review graph (T6) needs a task's checkable
criteria, but the decomposition is a non-deterministic LLM step, so it
MUST be computed once per task version and reused — otherwise two
attempts at the same task would be graded against different criteria
(a fairness defect, not just a cost one).

This service is the read-through entry point the graph calls
(``get_or_compute``):

1. **Content guard.** No active, ready ``DocumentSummary`` for the task
   (it is not ingested yet) → return ``None`` (not-ready: no LLM, no
   hard-fail). The caller degrades — criteria are simply unavailable,
   like a NULL ``mentor_preferences``.
2. **Version match.** An active ``TaskCriteria`` row whose version keys
   (``source_content_hash`` = the document's ``content_hash``,
   ``source_task_type`` = its ``task_type``) equal the current document
   → reuse it, zero LLM.
3. **Compute on miss.** Absent or stale version → decompose (LLM),
   release the old active row, insert the fresh one — the same
   release-old/insert-new reprocess discipline as DocumentSummary
   (Task 3.2.6).

MVP scope (sprint-mentor T4): compute + cache + reuse. The
author-editable slice of D11 is deferred; the author influences review
through ``author_mentor_notes`` (D6) until then. Eager warming at task
upload was deliberately NOT built — the trigger is lazy (this method).
The KD15 submit-time readiness gate is unchanged (DD-4-E); this content
guard is the read-side counterpart.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.criteria_decomposer import CriteriaDecomposerAgent
from course_supporter.homework.task_text import stitch_task_text
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
    TaskCriteria,
)
from course_supporter.storage.task_criteria_repository import TaskCriteriaRepository

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)


class CriteriaDecomposer(Protocol):
    """The LLM decomposition seam (satisfied by CriteriaDecomposerAgent)."""

    async def decompose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
    ) -> list[dict[str, str]]: ...


class CriteriaCacheService:
    """Read-through cache that computes task criteria once per version."""

    def __init__(self, session: AsyncSession, decomposer: CriteriaDecomposer) -> None:
        self._session = session
        self._repo = TaskCriteriaRepository(session)
        self._decomposer = decomposer

    async def get_or_compute(
        self, authored_document_id: uuid.UUID
    ) -> TaskCriteria | None:
        """Return the cached criteria for a task, computing them on miss.

        Args:
            authored_document_id: The task AuthoredDocument the criteria
                belong to.

        Returns:
            The active ``TaskCriteria`` row for the current task version,
            or ``None`` when the task is not reviewable yet (not a task,
            soft-deleted, or its DocumentSummary is not ready). ``None``
            means "no criteria, degrade" — never a hard failure.

        Raises:
            LadderExhaustedError: The decomposition ladder could not
                produce a valid result on a cache miss (propagated for
                the job's error handling).
        """
        doc = await self._session.get(AuthoredDocument, authored_document_id)
        if doc is None or doc.deleted_at is not None or doc.task_type is None:
            # Not a live task. The graph only calls this for tasks, but
            # guard defensively — no criteria, no LLM.
            return None
        if doc.content_hash is None:
            # The Merkle content_hash has not settled (task not fully
            # materialised) — treat as not-ready, no version key to match on.
            return None

        summary = await self._active_ready_summary(authored_document_id)
        if summary is None:
            # Content guard: the task is not ingested (no active, ready
            # DocumentSummary). Not-ready — no LLM, no hard-fail.
            return None

        existing = await self._repo.get_active(authored_document_id)
        if (
            existing is not None
            and existing.source_content_hash == doc.content_hash
            and existing.source_task_type == doc.task_type
        ):
            # Cache hit on the current task version — reuse, zero LLM.
            return existing

        # Miss (absent or stale version): compute, release old, insert new.
        task_text = await self._task_text(summary.id)
        criteria = await self._decomposer.decompose(
            task_title=summary.title,
            task_description=summary.description or "",
            task_text=task_text,
            task_type=doc.task_type,
            language=doc.language,
        )
        await self._repo.release_active(authored_document_id)
        row = await self._repo.create(
            authored_document_id=authored_document_id,
            criteria=criteria,
            source_content_hash=doc.content_hash,
            source_task_type=doc.task_type,
        )
        logger.info(
            "task_criteria_computed",
            authored_document_id=str(authored_document_id),
            task_type=doc.task_type,
            criteria_count=len(criteria),
            revised=existing is not None,
        )
        return row

    async def _active_ready_summary(
        self, authored_document_id: uuid.UUID
    ) -> DocumentSummary | None:
        """Active, ready DocumentSummary for the task (KD15 readiness signal)."""
        stmt = select(DocumentSummary).where(
            DocumentSummary.authored_document_id == authored_document_id,
            DocumentSummary.deleted_at.is_(None),
            DocumentSummary.status == "ready",
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def _task_text(self, summary_id: uuid.UUID) -> str:
        """Concatenate the task's active segment content in reading order."""
        stmt = (
            select(DocumentSegment.content)
            .where(
                DocumentSegment.document_summary_id == summary_id,
                DocumentSegment.deleted_at.is_(None),
            )
            .order_by(DocumentSegment.order)
        )
        result = await self._session.execute(stmt)
        # Budgeted stitch — same guard as load_task_context (the private
        # twin must not stay unguarded; task-code-materials commit 6).
        return stitch_task_text(result.scalars().all())


def build_criteria_cache_service(
    session: AsyncSession, stage_router: StageRouter
) -> CriteriaCacheService:
    """Wire the read-through cache with the production decomposer agent.

    The Mentor review job (T6) constructs the service from its
    ``StageRouter`` (ESC rows are already tagged with the job id via the
    ContextVar set at job entry), mirroring the methodist factory shape.
    """
    return CriteriaCacheService(session, CriteriaDecomposerAgent(stage_router))
