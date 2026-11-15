"""What a submission's result is, decided by its task type (mentor-rebuild task 07).

Purpose:
    The new path's body ends every revision the same way: it writes a result and
    delivers it. What that result IS depends on the task type — a test's is
    computed from its answers and its key, with no model call; a text task's
    (``task``, ``short_task``) is laid out from what its stages wrote, the
    verdicts and their explanation (task 09b); a project's is the text task's,
    by the same builder (project chain K6): its stages write the same rows and
    the same explanation, and the place of a problem in its files is already
    the builder's (K5). The body asks this module for the type's builder and
    knows its name, not its content (task 07, decision 6).

Interface:
    :class:`BuildContext` — what a builder is handed.
    :class:`BuiltResult` — the review structure, its markdown and the score.
    :class:`ResultBuilder` — the port: :meth:`~ResultBuilder.build` before the
    result is delivered, :meth:`~ResultBuilder.after_delivery` once it is.
    :class:`ResultNotBuiltError` — a builder's refusal, carrying the short code
    the submission fails with.
    :func:`get_result_builder` — the builder of a task type, or ``None`` when
    the type has none.
    :func:`validate_result_builders` — the startup check: a type the
    configuration serves by the new path without a builder stops the boot
    (project chain K6). The body would otherwise finish such a submission
    "reviewed" with no review written, and nothing would say why.

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

from course_supporter.homework.path_config import ServedBy
from course_supporter.models.source import AssignmentType

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.models.review_structure import ReviewStructureV1
    from course_supporter.storage.orm import HomeworkSubmission

__all__ = [
    "BuildContext",
    "BuiltResult",
    "ResultBuilder",
    "ResultNotBuiltError",
    "get_result_builder",
    "missing_result_builders",
    "validate_result_builders",
]


@dataclass(frozen=True, slots=True)
class BuildContext:
    """What a builder is handed by the body.

    ``session`` is the submission's own: a builder READS through it, because
    the body writes the result and a builder's stray write would ride on the
    same transaction. The one write that rides on it on purpose is a text
    task's course links as its review gives them
    (``homework/course_link_delivery.py``): they belong to that review and
    are committed with it, or not at all. ``session_factory`` is for
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

    The table is built on call rather than at import: the builders import
    this module for its types, and a table at import time would make them
    import each other.
    """
    from course_supporter.homework.test_result import TestResultBuilder
    from course_supporter.homework.text_result import TextResultBuilder

    builders: dict[AssignmentType, ResultBuilder] = {
        AssignmentType.TEST: TestResultBuilder(),
        AssignmentType.SHORT_TASK: TextResultBuilder(),
        AssignmentType.TASK: TextResultBuilder(),
        # A project's review has the text task's form (chain K6): verdicts
        # from its rows, the pass by ``pass_decision``, remarks with the place
        # of the problem in the student's files (K5), course links.
        AssignmentType.PROJECT: TextResultBuilder(),
    }
    return builders.get(task_type)


def missing_result_builders(config: PathConfig) -> list[AssignmentType]:
    """Types served by the new path that have no builder, in the enum's order.

    Empty means every type the new path serves ends with a written review.
    """
    return [
        task_type
        for task_type in AssignmentType
        if (declared := config.task_types.get(task_type)) is not None
        and declared.served_by is ServedBy.NEW_PATH
        and get_result_builder(task_type) is None
    ]


def validate_result_builders(config: PathConfig) -> None:
    """Refuse to boot when a type on the new path has no result builder.

    The twin of :func:`~course_supporter.homework.path_stages.validate_stage_executors`
    and called beside it in both processes (``boot.py``): a hole in the
    configuration stops the boot, never a submission. A type switched to the
    new path without a builder would walk its stages, pay for them, and end
    ``completed`` with no review for the student.

    Raises:
        ValueError: naming every such type at once.
    """
    missing = missing_result_builders(config)
    if missing:
        raise ValueError(
            "Task types served by the new path without a result builder: "
            + ", ".join(f"'{task_type.value}'" for task_type in missing)
            + ". Add one to get_result_builder in homework/result_builders.py, "
            "or serve the type by todays_mentor in config/submission_paths.yaml."
        )
