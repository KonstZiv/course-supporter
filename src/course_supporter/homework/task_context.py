"""Shared task-context loader for the homework pipeline (sprint-mentor T7).

Both the sanity gate (T7) and the review graph (T6) need the task's
title/description/text, derived the same way — from the task's active, ready
``DocumentSummary`` and its ordered segments. Extracted here so there is one
source of truth for "what is the task?" rather than two copies of the query.

A test is read differently: :func:`load_task_source_text` gives the text a
test's questions are parsed from, joined without the separator the models get
(mentor-rebuild task 07).
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


async def load_task_source_text(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> str:
    """The task's source text: its active segments joined WITHOUT a separator.

    Same summary, same segments, same order as :func:`load_task_context`, but
    not stitched. The segments of a text document are slices of one reference
    text, contiguous and covering it whole (the draft validators of
    ``ingestion/schemas.py`` refuse anything else), so joining them back with
    nothing between restores that text exactly — measured on the polygon's test
    (probe of task 07, section 3). The stitched text is for a model's context
    and puts a blank line at every boundary; the boundaries are chosen by a
    model and fall in the middle of a line, which cuts a question's text in two
    and could cut ``12.`` into ``1`` and ``2.``. A test is read from this text
    (``homework/test_text.py``), and so are its question numbers.

    No budget here. The caller decides what an over-long text means —
    ``reference_key.parse_question_numbers`` refuses it as truncated.

    The query is deliberately not shared with :func:`load_task_context`: that
    function feeds today's Mentor, which is not to be edited while it serves
    production (``TASK.md`` stop condition).

    Returns:
        The text, or ``""`` when the task has no active ready summary yet.
    """
    summary_id = (
        await session.execute(
            select(DocumentSummary.id).where(
                DocumentSummary.authored_document_id == authored_document_id,
                DocumentSummary.deleted_at.is_(None),
                DocumentSummary.status == "ready",
            )
        )
    ).scalar_one_or_none()
    if summary_id is None:
        return ""
    contents = await session.scalars(
        select(DocumentSegment.content)
        .where(
            DocumentSegment.document_summary_id == summary_id,
            DocumentSegment.deleted_at.is_(None),
        )
        .order_by(DocumentSegment.order)
    )
    return "".join(contents.all())
