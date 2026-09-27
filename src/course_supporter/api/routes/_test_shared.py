"""What the portal's and the channel's test routes share (mentor-rebuild tasks 07, 07b).

Both entries return a test the same way and let in only a published test
written in the system; one place each, so the two cannot drift. Their other
gates — who may ask, and whose task it is — differ by entry and stay in the
route modules, beside the file routes they mirror.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

from course_supporter.api.schemas import (
    TestStructureOption,
    TestStructureQuestion,
    TestStructureResponse,
)
from course_supporter.homework.test_doors import MISSING_TASK, published_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.test_doors import AnswerSheet
    from course_supporter.storage.orm import AuthoredDocument, TestVersion


async def require_published(
    session: AsyncSession, task_doc: AuthoredDocument
) -> TestVersion:
    """The test routes' gate: the version in force of a published written test.

    Anything else is a missing task, in the same bytes as one (task 07b,
    decisions 8 and 11): a test before its first publication, a test written as
    a file — no longer answered — and a task that is not a test at all. None of
    them says it exists.
    """
    published = await published_version(session, task_doc)
    if published is None:
        raise HTTPException(status_code=404, detail=MISSING_TASK)
    return published


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
