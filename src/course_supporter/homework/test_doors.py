"""The doors of a test: what is refused before anything is stored (tasks 07, 07b).

Purpose:
    A test is answered with a structure, and a structure can be wrong in ways a
    file cannot: meant for another version of the test, sent while tests are
    still answered with a file, or naming questions and options the test does
    not have. Each is refused HERE — before an upload and before a submission
    row (task 07, decision 12) — with a code of its own. A file sent to a test
    is refused at the file routes' door the same way.

    Since task 07b a test is answered from its published version (decisions 8,
    11, 14): the routes let in only a test written in the system and published
    — any other task is a missing one to them — and the doors read the version
    in force, which the stored answers then name.

Interface:
    :func:`new_path_serves_tests` — whether the ``test`` type is on the new path.
    :func:`published_version` — the version in force of a written test, and
    :func:`questions_of` — its questions as the doors and the review read them.
    :func:`answer_sheet` — the test as it is answered: questions and options,
    its version, whether answers are taken — and nothing of the key.
    :func:`refuse_a_file_for_a_test` and :func:`refuse_a_file_for_a_written_test`
    — the file routes' doors.
    :func:`check_test_answers` — the structure routes' door; returns the answers
    in canonical form, the form stored and scored.
    :func:`stored_answers` and :func:`read_stored_answers` — what a test
    submission is stored as, and reading it back.
    :class:`DoorCode` — the codes.

Every refusal is an ``HTTPException`` with ``{"code", "details"}``, as the
project preflight's refusals are (``homework/submission_core.py``): the code
crosses the boundary and the surface picks its own words; ``details`` is the
fallback it shows for a code it does not know yet.

The key is read from the version and never shown: a version carries it in its
marks, and nothing here writes anything, so a refused submission leaves no
trace anywhere.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException

from course_supporter.homework.path_config import ServedBy, get_path_config
from course_supporter.homework.test_object import PublishedBody
from course_supporter.homework.test_text import Option, Question, canonical_answers
from course_supporter.models.source import AssignmentType, SourceType
from course_supporter.storage.test_object_repository import TestObjectRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import AuthoredDocument, TestVersion

__all__ = [
    "MISSING_TASK",
    "AnswerSheet",
    "DoorCode",
    "answer_sheet",
    "check_test_answers",
    "new_path_serves_tests",
    "published_version",
    "questions_of",
    "read_stored_answers",
    "refuse_a_file_for_a_test",
    "refuse_a_file_for_a_written_test",
    "stored_answers",
]

MISSING_TASK: Final[str] = "Task not found."
"""The body every entry answers a task that is not there with (a plain 404)."""


class DoorCode(StrEnum):
    """Why a test submission was refused at the door (pre-flight section 6)."""

    TEST_ANSWERS_REQUIRED = "TEST_ANSWERS_REQUIRED"
    """A file sent to a test: a test is answered with its answers (422)."""

    TEST_FORM_UNAVAILABLE = "TEST_FORM_UNAVAILABLE"
    """Answers sent while tests are still reviewed by today's Mentor (409)."""

    NOT_A_TEST_TASK = "NOT_A_TEST_TASK"
    """Answers sent to a task that is not a test (422).

    Not given by the test routes since task 07b: a task that is not a published
    test written in the system is a missing one to them. Kept while the docs and
    the portal name it (task 07b, commit Zh1)."""

    TEST_VERSION_CHANGED = "TEST_VERSION_CHANGED"
    """The answers are for a version of the test that is not the current one (409)."""

    TEST_NOT_READY = "TEST_NOT_READY"
    """No author's key applies to the current version of the test (409).

    Not given since task 07b: a published version always carries its key, and
    an unpublished test is a missing one. Kept while the docs and the portal
    name it (task 07b, commit Zh1)."""

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


async def published_version(
    session: AsyncSession, task_doc: AuthoredDocument
) -> TestVersion | None:
    """The version in force of a test written in the system — the latest published.

    ``None`` before its first publication, and for any other task: a test
    written as a file is not answered since task 07b (decision 11), and a task
    of another type was never a test.
    """
    if task_doc.source_type != SourceType.TEST_OBJECT.value:
        return None
    return await TestObjectRepository(session).latest_version(task_doc.id)


@dataclass(frozen=True, slots=True)
class AnswerSheet:
    """A test as it is answered: its version, whether answers are taken, questions.

    ``questions`` are the version's own — each number, text and option with its
    letter. Nothing of the key is here: not which options are right, not how
    many, not an explanation, a doubt or the pass mark (task 07, invariant 5
    and decision 22).
    """

    version: str
    accepting_answers: bool
    questions: tuple[Question, ...]


def answer_sheet(published: TestVersion) -> AnswerSheet:
    """The test as a student is shown it, before answering.

    ``version`` is the version's visible digest, the one the answers are sent
    back with; ``accepting_answers`` is the switch a submission's doors will
    ask. A published version always carries its key, so nothing else holds
    answers back.
    """
    return AnswerSheet(
        version=published.content_digest,
        accepting_answers=new_path_serves_tests(),
        questions=questions_of(published),
    )


def refuse_a_file_for_a_test(task_doc: AuthoredDocument) -> None:
    """Refuse a file sent to a test — once the test is answered by its answers.

    Stands after the task's own checks and before the upload, so nothing is
    stored for a refused file. With the test type still on today's Mentor it
    lets the file through, exactly as before task 07: that is the way back if
    the switch is turned off.
    """
    if task_doc.task_type != AssignmentType.TEST.value or not new_path_serves_tests():
        return
    raise _answers_required()


async def refuse_a_file_for_a_written_test(
    session: AsyncSession, task_doc: AuthoredDocument
) -> None:
    """A test written in the system is never answered with a file (task 07b).

    Unpublished, it is a missing task — the 404 every other absence gets;
    published, the file is refused with ``TEST_ANSWERS_REQUIRED``, whatever the
    switch says, because no reviewer but the new path can read a written test.
    Called before the file routes' readiness gate: a written test is never
    processed, so that gate would answer 409 about a summary it will never have
    (PRE-FLIGHT section 9, row 5). Any other task passes untouched.
    """
    if task_doc.source_type != SourceType.TEST_OBJECT.value:
        return
    if await published_version(session, task_doc) is None:
        raise HTTPException(status_code=404, detail=MISSING_TASK)
    raise _answers_required()


def check_test_answers(
    published: TestVersion,
    answers: Mapping[str, Sequence[str]],
    test_version: str | None,
) -> dict[str, list[str]]:
    """Refuse what cannot be a submission to this version; return the answers canonical.

    The checks go from the test to the answers, the order in which a cause
    makes sense to whoever sent them: the form not served yet, another version
    of the test, and only then the answers themselves. A version that differs
    only in its pass mark or the author's own explanations has the same visible
    digest, so answers to the form the student saw are still taken — and
    scored by the version in force now, which the stored answers name. An
    unanswered question is not refused: it is counted wrong (task 07,
    decision 23).
    """
    if not new_path_serves_tests():
        raise _refusal(
            409,
            DoorCode.TEST_FORM_UNAVAILABLE,
            "Tests are not taken as answers yet: send the work as a file.",
        )
    if test_version is not None and test_version != published.content_digest:
        raise _refusal(
            409,
            DoorCode.TEST_VERSION_CHANGED,
            "The test has changed since these answers were given: read it again "
            "and send the answers to its current version.",
        )

    canonical = canonical_answers(answers)
    options = {
        question.number: {option.key for option in question.options}
        for question in questions_of(published)
    }
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


def stored_answers(version_id: uuid.UUID, answers: Mapping[str, list[str]]) -> str:
    """The text a test submission is stored as (task 07b, decision 14).

    The version the doors took the answers for, and the answers in canonical
    form, as canonical JSON: keys sorted, no incidental whitespace, letters
    unescaped. The same answers to the same version are the same bytes, so
    ``file_hash`` means what it says.

    >>> version = uuid.UUID(int=7)
    >>> stored_answers(version, {"1": ["b"]})
    '{"answers":{"1":["b"]},"version_id":"00000000-0000-0000-0000-000000000007"}'
    >>> read_stored_answers(stored_answers(version, {"1": ["b"]}))
    (UUID('00000000-0000-0000-0000-000000000007'), {'1': ['b']})
    """
    return json.dumps(
        {"answers": dict(answers), "version_id": str(version_id)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def read_stored_answers(text: str) -> tuple[uuid.UUID, dict[str, list[str]]]:
    """The version and the answers a stored test submission names.

    Raises:
        ValueError: the text is not what :func:`stored_answers` writes — the
            project's own fault, never the student's.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict) or set(raw) != {"answers", "version_id"}:
        msg = "the stored answers are not a version and its answers"
        raise ValueError(msg)
    answers = raw["answers"]
    if not isinstance(answers, dict) or not all(
        isinstance(labels, list) and all(isinstance(label, str) for label in labels)
        for labels in answers.values()
    ):
        msg = "the stored answers are not a mapping of question to labels"
        raise ValueError(msg)
    return uuid.UUID(str(raw["version_id"])), {
        str(number): list(labels) for number, labels in answers.items()
    }


def questions_of(published: TestVersion) -> tuple[Question, ...]:
    """The version's questions as the doors and the review read them.

    Task 07's carriers — number, text, options with their labels — so the form,
    the doors and the review's own functions read a written test as they read
    every test before it.
    """
    body = PublishedBody.from_jsonb(published.body)
    return tuple(
        Question(
            number=question.number,
            text=question.text,
            options=tuple(
                Option(label=option.label, text=option.text)
                for option in question.options
            ),
        )
        for question in body.questions
    )


def _answers_required() -> HTTPException:
    return _refusal(
        422,
        DoorCode.TEST_ANSWERS_REQUIRED,
        "This task is a test: it is answered with the answers to its questions, "
        "not with a file.",
    )


def _refusal(status: int, code: DoorCode, details: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": code.value, "details": details}
    )
