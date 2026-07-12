"""Shared task-context loader for the homework pipeline (sprint-mentor T7).

Both the sanity gate (T7) and the review graph (T6) need the task's
title/description/text, derived the same way — from the task's active, ready
``DocumentSummary`` and its ordered segments. Extracted here so there is one
source of truth for "what is the task?" rather than two copies of the query.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.task_text import stitch_task_text
from course_supporter.storage.orm import DocumentSegment, DocumentSummary


async def load_task_context(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> tuple[str, str, str]:
    """Task title/description/text from the active ready DocumentSummary.

    Returns ``("", "", "")`` when the task is not ingested yet (degrade — the
    consumer still runs with minimal grounding, never hard-fails). The submit
    route enforces readiness up front (KD15 §1319), so by the time the worker
    reaches here the summary normally exists; this stays tolerant as
    defense-in-depth.
    """
    stmt = select(DocumentSummary).where(
        DocumentSummary.authored_document_id == authored_document_id,
        DocumentSummary.deleted_at.is_(None),
        DocumentSummary.status == "ready",
    )
    result = await session.execute(stmt)
    summary = result.scalar_one_or_none()
    if summary is None:
        return "", "", ""
    text_stmt = (
        select(DocumentSegment.content)
        .where(
            DocumentSegment.document_summary_id == summary.id,
            DocumentSegment.deleted_at.is_(None),
        )
        .order_by(DocumentSegment.order)
    )
    text_result = await session.execute(text_stmt)
    # Budgeted stitch (task-code-materials commit 6): code segments are
    # verbatim source files — the unguarded join would blow the mentor
    # stages' context windows on project-sized tasks.
    task_text = stitch_task_text(text_result.scalars().all())
    return summary.title or "", summary.description or "", task_text
