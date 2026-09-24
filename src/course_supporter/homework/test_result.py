"""A test's review, built from the answers and the key — no model call (task 07).

Purpose:
    The result builder of the ``test`` type (``homework/result_builders.py``).
    Everything a test review says is decided by code: which answers are right
    (``homework/test_scoring.py``), what the score is, whether the pass mark is
    met, and which explanation stands under a wrong answer. The words around
    them come from the phrasebook through the one assembler. The work of a
    submission pays for nothing (task 07, invariant 1).

The order, and why each step is where it is (pre-flight 7.4):
    1. The student's canonical answers, from the file the doors read.
    2. The key and its explanations in the course language and in the review's
       — READ, never written (:meth:`ReferenceService.explanations_for`).
    3. The pure functions: check, score, verdict, which explanation.
    4. The structure (:class:`ReviewStructureV1` with a test section).
    5. Its markdown, by the assembler.
    After delivery, :meth:`TestResultBuilder.after_delivery` asks for the
    explanations in the student's language.

How the review survives another job of the task (task 07, decision 9):
    The database keeps one job in flight per task, and carrying the key onto a
    new text asks for one. So the review never carries anything: it reads, and
    a key that would be carried reads as it will stand — the same answers and
    the same pass mark — with no model explanations for the new text yet, which
    is also all a version made at that moment could offer. The carrying and
    the asking happen after delivery, in a session of their own; a collision
    there is rolled back and logged, and the delivered review is not touched.
    The next submission asks again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import structlog

from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.reference_service import (
    ExplanationsView,
    GenerationInProgressError,
    ReadOnlyQueue,
    ReferenceRefusedError,
    ReferenceService,
)
from course_supporter.homework.result_builders import (
    BuildContext,
    BuiltResult,
    ResultNotBuiltError,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.task_context import load_task_source_text
from course_supporter.homework.test_scoring import (
    choose_explanation,
    pass_verdict,
    score_answers,
)
from course_supporter.homework.test_text import (
    ParsedTest,
    canonical_label,
    parse_test,
)
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    ReviewStructureV1,
    TestOption,
    TestQuestionResult,
    TestSection,
    Verdict,
)
from course_supporter.phrasebook import FALLBACK_LANGUAGE
from course_supporter.storage.orm import AuthoredDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.test_text import Option

__all__ = ["TEST_NOT_READY", "TestResultBuilder", "build_test_review"]

logger = structlog.get_logger(__name__)

TEST_NOT_READY = "test_not_ready"
"""The code a submission fails with when no key applies to the test any more.

The doors let the submission in because a key applied then; the author cleared
or changed it before the work ran (pre-flight 7.5). The portal reads this code
as it reads the door's own refusal of the same name.
"""


def build_test_review(
    *,
    student_answers: Mapping[str, Sequence[str]],
    test: ParsedTest,
    course: ExplanationsView,
    review: ExplanationsView,
) -> tuple[ReviewStructureV1, int]:
    """A test's review as a structure, and its score — a pure function.

    ``course`` and ``review`` are the same key read in the course language and
    in the review's language; they are one and the same view when the two
    languages are one. The key, the pass mark and the author's explanations are
    the key's own and read from ``course``; the model's explanations and doubts
    are per language.

    A wrong answer shows the correct option as the student saw it — the
    author's label and the option's text, found in ``test`` by canonical label
    — and the explanation ``test_scoring.choose_explanation`` picks. A doubt in
    either language hides the model's words. The line
    "explanations are in the course language" is decided here, not by the
    chooser: it is said only when the review is in another language and one of
    the explanations shown is in the course language.
    """
    key = course.answers
    sheet = score_answers(key, student_answers)
    decided = pass_verdict(sheet.score, course.pass_threshold)
    same_language = review.language == course.language
    options = _options_by_question(test)

    questions: list[TestQuestionResult] = []
    shown_in_course_language = False
    for number, right in sheet.checks.items():
        if right:
            questions.append(TestQuestionResult(number=number, correct=True))
            continue
        chosen = choose_explanation(
            correct=False,
            author=course.author.get(number),
            doubted=bool(course.doubts.get(number) or review.doubts.get(number)),
            in_review_language=review.model.get(number),
            in_course_language=None if same_language else course.model.get(number),
        )
        explanation = chosen.text if chosen is not None else None
        if chosen is not None and explanation is not None:
            shown_in_course_language |= chosen.in_course_language
        questions.append(
            TestQuestionResult(
                number=number,
                correct=False,
                correct_answer=[
                    _shown_option(label, options.get(number, ()))
                    for label in key[number]
                ],
                explanation=explanation,
            )
        )

    structure = ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=review.language,
        verdict=None if decided is None else Verdict(passed=decided),
        test=TestSection(
            score=sheet.score,
            questions=questions,
            explanations_in_course_language=(
                shown_in_course_language and not same_language
            ),
            retry_offer=decided is False,
        ),
    )
    return structure, sheet.score


class TestResultBuilder:
    """The result of a test: scored by code, explained from the reference."""

    async def build(self, context: BuildContext) -> BuiltResult:
        """Read the answers and the key, and write the review out.

        Raises:
            ResultNotBuiltError: no key applies to the test any more
                (:data:`TEST_NOT_READY`).
            ValueError: the stored answers are not the shape the core writes —
                our own fault, and the body fails the submission with its
                generic code.
        """
        document_id = context.submission.authored_document_id
        course_language, review_language = await _languages(
            context.session, document_id, context.review_language
        )
        student_answers = _read_answers(context.submission_text)

        service = ReferenceService(context.session, ReadOnlyQueue())
        try:
            course = await service.explanations_for(document_id, course_language)
            review = (
                course
                if review_language == course_language
                else await service.explanations_for(document_id, review_language)
            )
        except ReferenceRefusedError as exc:
            raise ResultNotBuiltError(TEST_NOT_READY, exc.details) from exc
        if not course.answers:
            raise ResultNotBuiltError(
                TEST_NOT_READY,
                "no answer key applies to the current version of the test",
            )

        test = parse_test(await load_task_source_text(context.session, document_id))
        structure, score = build_test_review(
            student_answers=student_answers, test=test, course=course, review=review
        )
        return BuiltResult(
            structure=structure, markdown=assemble_review(structure), score=score
        )

    async def after_delivery(self, context: BuildContext) -> None:
        """Ask for the explanations in the student's language, in a session of its own.

        :meth:`ReferenceService.request_explanations` carries the key onto a
        new text and asks for work once per version and language. Another job
        of the task in flight is an ordinary state, not a failure: the session
        is rolled back — no version is left without its job — and the next
        submission asks again. Anything else propagates to the body, which logs
        it: the review is already out, and a failed request must not take it
        back.
        """
        submission = context.submission
        if context.redis is None:
            logger.info(
                "test_explanations_not_requested",
                submission_id=str(submission.id),
                reason="no_queue",
            )
            return
        async with context.session_factory() as session:
            _, review_language = await _languages(
                session, submission.authored_document_id, context.review_language
            )
            queue = ArqExplanationQueue(
                redis=context.redis, session=session, tenant_id=submission.tenant_id
            )
            try:
                await ReferenceService(session, queue).request_explanations(
                    submission.authored_document_id, review_language
                )
                await session.commit()
            except GenerationInProgressError:
                await session.rollback()
                logger.info(
                    "test_explanations_request_deferred",
                    submission_id=str(submission.id),
                    language=review_language,
                )


async def _languages(
    session: AsyncSession, document_id: uuid.UUID, resolved: str | None
) -> tuple[str, str]:
    """The course language and the review's, each a code a structure can carry.

    The review's is the doors' resolution; with none, the review is in the
    course language — the language its explanations are written in.
    """
    document = await session.get(AuthoredDocument, document_id)
    course = (document.language if document is not None else None) or FALLBACK_LANGUAGE
    return course, resolved or course


def _read_answers(text: str) -> dict[str, list[str]]:
    """The canonical answers the core stored, checked for shape before use."""
    raw = json.loads(text)
    if not isinstance(raw, dict) or not all(
        isinstance(labels, list) and all(isinstance(label, str) for label in labels)
        for labels in raw.values()
    ):
        msg = "the stored answers are not a mapping of question to labels"
        raise ValueError(msg)
    return {str(number): list(labels) for number, labels in raw.items()}


def _options_by_question(test: ParsedTest) -> dict[str, tuple[Option, ...]]:
    """Each question's options, by number; a repeated number keeps its first."""
    options: dict[str, tuple[Option, ...]] = {}
    for question in test.questions:
        options.setdefault(question.number, question.options)
    return options


def _shown_option(label: str, options: Sequence[Option]) -> TestOption:
    """A key label as the student saw it: the test's own label and its text.

    Found by canonical label, so a key typed in Latin still points at the
    Cyrillic option. A label the test does not have — the key was checked for
    its question numbers, not its labels — is shown as the author wrote it,
    with no text.
    """
    wanted = canonical_label(label)
    for option in options:
        if option.key == wanted:
            return TestOption(label=option.label, text=option.text)
    return TestOption(label=label, text="")
