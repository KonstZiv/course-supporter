"""The source text a test is read from, against a live database (task 07).

``load_task_source_text`` joins a task's active segments without a separator,
and the claim worth a database is that the rows it reads are the right ones:
the active ready summary, its live segments, in segment order. The headline
case is the one the probe of task 07 found on production — a segment boundary
chosen in the middle of a line — pushed to where it does damage: between the
``1`` and the ``2.`` of question 12.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.reference_key import parse_question_numbers
from course_supporter.homework.task_context import (
    load_task_context,
    load_task_source_text,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
)

pytestmark = pytest.mark.requires_db

_TWELVE_QUESTIONS = "\n".join(f"{n}. Питання {n}?\nа) так\nб) ні" for n in range(1, 13))


async def _task_with_segments(
    session: AsyncSession,
    root: CourseNode,
    pieces: list[str],
    *,
    status: str = "ready",
) -> tuple[AuthoredDocument, list[DocumentSegment]]:
    """A test task whose summary holds ``pieces`` as consecutive segments."""
    document = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url="file:///tmp/test.md",
        task_type="test",
        language="ukr",
        content_hash="v1" + "0" * 62,
    )
    session.add(document)
    await session.flush()

    summary = DocumentSummary(
        authored_document_id=document.id,
        course_root_id=root.id,
        title="Тест",
        status=status,
    )
    session.add(summary)
    await session.flush()

    segments: list[DocumentSegment] = []
    start = 0
    for order, piece in enumerate(pieces):
        segment = DocumentSegment(
            document_summary_id=summary.id,
            course_root_id=root.id,
            order=order,
            content=piece,
            description=f"segment {order}",
            start_pos=start,
            end_pos=start + len(piece),
        )
        start += len(piece)
        segments.append(segment)
    # Inserted in reverse on purpose: the text must follow ``order``, not the
    # order in which rows happened to reach the table.
    session.add_all(reversed(segments))
    await session.flush()
    return document, segments


class TestTheSourceTextIsTheTextTheSegmentsCut:
    async def test_a_boundary_inside_12_loses_no_number(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """The loader gives question 12 back; the stitched text does not."""
        cut = _TWELVE_QUESTIONS.index("12.") + 1
        document, _ = await _task_with_segments(
            db_session,
            seed_root_node,
            [_TWELVE_QUESTIONS[:cut], _TWELVE_QUESTIONS[cut:]],
        )

        _, _, stitched = await load_task_context(db_session, document.id)
        assert "12" not in parse_question_numbers(stitched).numbers, (
            "the premise: the stitched text of the same rows loses question 12"
        )

        source = await load_task_source_text(db_session, document.id)
        assert source == _TWELVE_QUESTIONS
        assert parse_question_numbers(source).numbers[-1] == "12"

    async def test_segments_join_in_their_order(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document, _ = await _task_with_segments(
            db_session, seed_root_node, ["1. Пер", "ше?\nа) так", "\n2. Друге?"]
        )
        assert (
            await load_task_source_text(db_session, document.id)
            == "1. Перше?\nа) так\n2. Друге?"
        )

    async def test_a_deleted_segment_is_not_part_of_the_text(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document, segments = await _task_with_segments(
            db_session, seed_root_node, ["1. Перше?\n", "стара вставка\n", "2. Друге?"]
        )
        segments[1].deleted_at = datetime.now(UTC)
        await db_session.flush()

        assert (
            await load_task_source_text(db_session, document.id)
            == "1. Перше?\n2. Друге?"
        )

    async def test_a_summary_that_is_not_ready_gives_no_text(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """No ready summary is "not ingested yet": an empty text, never an error."""
        document, _ = await _task_with_segments(
            db_session, seed_root_node, ["1. Перше?"], status="pending"
        )
        assert await load_task_source_text(db_session, document.id) == ""
