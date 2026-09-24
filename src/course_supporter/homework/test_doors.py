"""The doors of a test: what is refused before anything is stored (task 07).

Purpose:
    A test is answered with a structure, and a structure can be wrong in ways a
    file cannot: meant for another version of the test, sent before the
    author's key is ready for it, or naming questions and options the test does
    not have. Each is refused HERE — before an upload and before a submission
    row (task 07, decision 12) — with a code of its own. A file sent to a test
    is refused at the file routes' door the same way.

Interface:
    :func:`new_path_serves_tests` — whether the ``test`` type is on the new path.
    :func:`refuse_a_file_for_a_test` — the file routes' door.
    :func:`check_test_answers` — the structure routes' door; returns the answers
    in canonical form, the form stored and scored.
    :class:`DoorCode` — the codes.

Every refusal is an ``HTTPException`` with ``{"code", "details"}``, as the
project preflight's refusals are (``homework/submission_core.py``): the code
crosses the boundary and the surface picks its own words; ``details`` is the
fallback it shows for a code it does not know yet.

The key is read, never written: :meth:`ReferenceService.key_applies` answers
without carrying anything or asking for work, so a refused submission leaves
no trace anywhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi import HTTPException

from course_supporter.homework.path_config import ServedBy, get_path_config
from course_supporter.homework.reference_service import (
    ReadOnlyQueue,
    ReferenceRefusedError,
    ReferenceService,
)
from course_supporter.homework.task_context import load_task_source_text
from course_supporter.homework.test_text import canonical_answers, parse_test
from course_supporter.models.source import AssignmentType

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import AuthoredDocument

__all__ = [
    "DoorCode",
    "check_test_answers",
    "new_path_serves_tests",
    "refuse_a_file_for_a_test",
]


class DoorCode(StrEnum):
    """Why a test submission was refused at the door (pre-flight section 6)."""

    TEST_ANSWERS_REQUIRED = "TEST_ANSWERS_REQUIRED"
    """A file sent to a test: a test is answered with its answers (422)."""

    TEST_FORM_UNAVAILABLE = "TEST_FORM_UNAVAILABLE"
    """Answers sent while tests are still reviewed by today's Mentor (409)."""

    NOT_A_TEST_TASK = "NOT_A_TEST_TASK"
    """Answers sent to a task that is not a test (422)."""

    TEST_VERSION_CHANGED = "TEST_VERSION_CHANGED"
    """The answers are for a version of the test that is not the current one (409)."""

    TEST_NOT_READY = "TEST_NOT_READY"
    """No author's key applies to the current version of the test (409)."""

    ANSWERS_DO_NOT_MATCH_TEST = "ANSWERS_DO_NOT_MATCH_TEST"
    """A question or an option the test does not have (422)."""


def new_path_serves_tests() -> bool:
    """Whether the ``test`` type is reviewed on the new path.

    The switch in ``config/submission_paths.yaml``. While it is off, a test is
    reviewed by today's Mentor, which reads a file — so a file stays a test's
    form, and answers have nowhere to go.
    """
    declared = get_path_config().task_types.get(AssignmentType.TEST)
    return declared is not None and declared.served_by is ServedBy.NEW_PATH


def refuse_a_file_for_a_test(task_doc: AuthoredDocument) -> None:
    """Refuse a file sent to a test — once the test is answered by its answers.

    Stands after the task's own checks and before the upload, so nothing is
    stored for a refused file. With the test type still on today's Mentor it
    lets the file through, exactly as before task 07: that is the way back if
    the switch is turned off.
    """
    if task_doc.task_type != AssignmentType.TEST.value or not new_path_serves_tests():
        return
    raise _refusal(
        422,
        DoorCode.TEST_ANSWERS_REQUIRED,
        "This task is a test: it is answered with the answers to its questions, "
        "not with a file.",
    )


async def check_test_answers(
    session: AsyncSession,
    task_doc: AuthoredDocument,
    answers: Mapping[str, Sequence[str]],
    test_version: str | None,
) -> dict[str, list[str]]:
    """Refuse what cannot be a submission to this test; return the answers canonical.

    The checks go from the task to the answers, the order in which a cause
    makes sense to whoever sent them: the form not served yet, a task that is
    not a test, another version of it, no key for this version, and only then
    the answers themselves. An unanswered question is not refused — it is
    counted wrong (task 07, decision 23).
    """
    if not new_path_serves_tests():
        raise _refusal(
            409,
            DoorCode.TEST_FORM_UNAVAILABLE,
            "Tests are not taken as answers yet: send the work as a file.",
        )
    if task_doc.task_type != AssignmentType.TEST.value:
        raise _refusal(422, DoorCode.NOT_A_TEST_TASK, "This task is not a test.")
    if test_version is not None and test_version != task_doc.content_hash:
        raise _refusal(
            409,
            DoorCode.TEST_VERSION_CHANGED,
            "The test has changed since these answers were given: read it again "
            "and send the answers to its current version.",
        )
    if not await _key_applies(session, task_doc.id):
        raise _refusal(
            409,
            DoorCode.TEST_NOT_READY,
            "The test is not ready to be checked yet: its answer key has not "
            "been set for this version.",
        )

    canonical = canonical_answers(answers)
    test = parse_test(await load_task_source_text(session, task_doc.id))
    options: dict[str, set[str]] = {}
    for question in test.questions:
        options.setdefault(question.number, {option.key for option in question.options})
    unknown_questions = sorted(set(canonical) - set(options))
    unknown_options = sorted(
        f"{number}: {label}"
        for number, labels in canonical.items()
        if number in options
        for label in labels
        if label not in options[number]
    )
    if unknown_questions or unknown_options:
        raise _refusal(
            422,
            DoorCode.ANSWERS_DO_NOT_MATCH_TEST,
            f"The answers name what the test does not have: questions "
            f"{unknown_questions}, options {unknown_options}.",
        )
    return canonical


async def _key_applies(session: AsyncSession, document_id: uuid.UUID) -> bool:
    """The service's read-only answer; a task that cannot carry a key has none."""
    try:
        return await ReferenceService(session, ReadOnlyQueue()).key_applies(document_id)
    except ReferenceRefusedError:
        return False


def _refusal(status: int, code: DoorCode, details: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": code.value, "details": details}
    )
