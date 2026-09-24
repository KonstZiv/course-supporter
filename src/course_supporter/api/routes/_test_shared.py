"""What the portal's and the channel's test routes share (mentor-rebuild task 07).

Both entries return a test the same way and gate it on readiness the same way;
one place each, so the two cannot drift. Their other gates — who may ask, and
whose task it is — differ by entry and stay in the route modules, beside the
file routes they mirror.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi import HTTPException

from course_supporter.api.schemas import (
    TestStructureOption,
    TestStructureQuestion,
    TestStructureResponse,
)
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.test_doors import AnswerSheet

TASK_NOT_READY: Final[str] = (
    "Task is not ready for submissions yet (its summary has not been generated)."
)
"""The file routes' own readiness refusal, word for word."""


async def require_ready(session: AsyncSession, document_id: uuid.UUID) -> None:
    """The file routes' readiness gate: 409 until the task's summary is ready."""
    summary = await DocumentSummaryRepository(session).get_by_authored_document_id(
        document_id
    )
    if summary is None or summary.status != "ready":
        raise HTTPException(status_code=409, detail=TASK_NOT_READY)


def structure_response(sheet: AnswerSheet) -> TestStructureResponse:
    """The answer sheet as both entries return it — three fields, no key."""
    return TestStructureResponse(
        version=sheet.version,
        accepting_answers=sheet.accepting_answers,
        questions=[
            TestStructureQuestion(
                number=question.number,
                text=question.text,
                options=[
                    TestStructureOption(label=option.label, text=option.text)
                    for option in question.options
                ],
            )
            for question in sheet.questions
        ],
    )
