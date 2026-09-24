"""What a submission's result is, decided by its task type (mentor-rebuild task 07).

Purpose:
    The new path's body ends every revision the same way: it writes a result and
    delivers it. What that result IS depends on the task type — a test's is
    computed from its answers and its key, with no model call; the types whose
    stages judge will build theirs from what their stages wrote. The body asks
    this module for the type's builder and knows its name, not its content
    (task 07, decision 6).

Interface:
    :class:`BuildContext` — what a builder is handed.
    :class:`BuiltResult` — the review structure, its markdown and the score.
    :class:`ResultBuilder` — the port: :meth:`~ResultBuilder.build` before the
    result is delivered, :meth:`~ResultBuilder.after_delivery` once it is.
    :class:`ResultNotBuiltError` — a builder's refusal, carrying the short code
    the submission fails with.
    :func:`get_result_builder` — the builder of a task type, or ``None`` when
    the type has none and its path ends without a written review.

Adding or replacing a builder:
    A type gets a builder by a line in :func:`get_result_builder`'s table and a
    class with the two methods of :class:`ResultBuilder`; the body does not
    change. A test replaces the shipped one by patching
    :func:`get_result_builder` where the body looks it up
    (``homework/path_runner.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from course_supporter.models.source import AssignmentType

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.models.review_structure import ReviewStructureV1
    from course_supporter.storage.orm import HomeworkSubmission

__all__ = [
    "BuildContext",
    "BuiltResult",
    "ResultBuilder",
    "ResultNotBuiltError",
    "get_result_builder",
]


@dataclass(frozen=True, slots=True)
class BuildContext:
    """What a builder is handed by the body.

    ``session`` is the submission's own: a builder READS through it and writes
    nothing, because the body writes the result and a builder's stray write
    would ride on the same transaction. ``session_factory`` is for
    :meth:`ResultBuilder.after_delivery`, whose writes get sessions of their
    own. ``submission_text`` is what the doors read — for a test, its canonical
    answers. ``review_language`` is the doors' resolution, ``None`` when no
    source gave one. ``redis`` is the queue a request for work goes through, and
    ``None`` where there is none to go through.
    """

    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]
    submission: HomeworkSubmission
    submission_text: str
    review_language: str | None
    redis: ArqRedis | None


@dataclass(frozen=True, slots=True)
class BuiltResult:
    """A review, as the body stores it: structure, markdown, score.

    The markdown comes from the one assembler (``homework/review_assembler.py``):
    the text of a review is born nowhere else (task 07, invariant 6).
    """

    structure: ReviewStructureV1
    markdown: str
    score: int | None


class ResultNotBuiltError(Exception):
    """A builder could not build the result; ``code`` is why, for the surface.

    A code, not a sentence: the submission fails with it as its
    ``error_message``, and the portal phrases it. Anything else a builder
    raises fails the submission with the body's own generic code.
    """

    def __init__(self, code: str, details: str) -> None:
        super().__init__(f"{code}: {details}")
        self.code = code
        self.details = details


class ResultBuilder(Protocol):
    """The port the body builds a result through.

    ``build`` runs before delivery and returns what is stored and sent; an
    exception from it fails the submission. ``after_delivery`` runs once the
    result is out — the place for anything that may wait or may fail without
    taking back what the student already has; the body logs what it raises and
    goes on.
    """

    async def build(self, context: BuildContext) -> BuiltResult: ...

    async def after_delivery(self, context: BuildContext) -> None: ...


def get_result_builder(task_type: AssignmentType) -> ResultBuilder | None:
    """The builder of ``task_type``'s result, or ``None`` when it has none.

    The table is built on call rather than at import: the test builder imports
    this module for its types, and a table at import time would make the two
    modules import each other.
    """
    from course_supporter.homework.test_result import TestResultBuilder

    builders: dict[AssignmentType, ResultBuilder] = {
        AssignmentType.TEST: TestResultBuilder(),
    }
    return builders.get(task_type)
