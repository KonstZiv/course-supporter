"""A test's review, built from the answers and the key — no model call (tasks 07, 07b).

Purpose:
    The result builder of the ``test`` type (``homework/result_builders.py``).
    Everything a test review says is decided by code: which answers are right
    (``homework/test_scoring.py``), what the score is, whether the pass mark is
    met, and which explanation stands under a wrong answer. The words around
    them come from the phrasebook through the one assembler. The work of a
    submission pays for nothing (task 07, invariant 1).

The order, and why each step is where it is (pre-flight 7.4):
    1. The student's canonical answers and the published version the doors
       took them for, from the file the core stored (task 07b, decision 14).
    2. That version's key, pass mark and the author's own explanations, and
       the model's explanations for its axes in the course language and in
       the review's — READ, never written
       (:meth:`ReferenceService.explanations_of`).
    3. The pure functions: check, score, verdict, which explanation.
    4. The structure (:class:`ReviewStructureV1` with a test section).
    5. Its markdown, by the assembler.
    After delivery, :meth:`TestResultBuilder.after_delivery` asks for the
    explanations in the student's language, for the same version.

How the review survives another job of the task (task 07, decision 9):
    The database keeps one job in flight per task, and asking for explanations
    takes one. So the review never asks: it reads what is written, and the
    asking happens after delivery, in a session of its own; a collision there
    is rolled back and logged, and the delivered review is not touched. The
    next submission asks again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import structlog

from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.reference_service import (
    ExplanationsView,
    GenerationInProgressError,
    ReadOnlyQueue,
    ReferenceService,
)
from course_supporter.homework.result_builders import BuildContext, BuiltResult
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.test_doors import questions_of, read_stored_answers
from course_supporter.homework.test_object_service import TestObjectService
from course_supporter.homework.test_scoring import (
    choose_explanation,
    pass_verdict,
    score_answers,
)
from course_supporter.homework.test_text import ParsedTest, canonical_label
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
from course_supporter.storage.test_object_repository import TestObjectRepository

if TYPE_CHECKING:
    from course_supporter.homework.test_text import Option

__all__ = ["TEST_NOT_READY", "TestResultBuilder", "build_test_review"]

logger = structlog.get_logger(__name__)

TEST_NOT_READY = "test_not_ready"
"""The code a submission failed with when no key applied to the test any more.

Not given since task 07b: a submission is scored by the version it was taken
for, which always carries its key. Kept while the docs and the README's lock
name it (task 07b, commit Zh1).
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
        """Read the answers and the version they were taken for, and write the review.

        The version is the one the doors bound the answers to, not the newest:
        a version published while the submission waited does not re-score it
        (task 07b, decision 14). Its key, pass mark and the author's own
        explanations come with it; the model's explanations are read for its
        axes, in the course language and the review's.

        Raises:
            ValueError: the stored answers are not what the core writes, or the
                version they name is not there — our own fault, and the body
                fails the submission with its generic code.
        """
        version_id, student_answers = read_stored_answers(context.submission_text)
        published = await TestObjectRepository(context.session).get_version(version_id)
        document = (
            await context.session.get(AuthoredDocument, published.authored_document_id)
            if published is not None
            else None
        )
        if published is None or document is None:
            msg = f"the version {version_id} the answers were taken for is not there"
            raise ValueError(msg)
        course_language = published.language
        review_language = context.review_language or course_language

        service = ReferenceService(context.session, ReadOnlyQueue())
        course = await service.explanations_of(document, published, course_language)
        review = (
            course
            if review_language == course_language
            else await service.explanations_of(document, published, review_language)
        )

        structure, score = build_test_review(
            student_answers=student_answers,
            test=ParsedTest(questions=questions_of(published)),
            course=course,
            review=review,
        )
        return BuiltResult(
            structure=structure, markdown=assemble_review(structure), score=score
        )

    async def after_delivery(self, context: BuildContext) -> None:
        """Ask for the explanations in the student's language, in a session of its own.

        For the version the submission was taken for, through the object's
        service (task 07b, decision 14), once per version and language. Another
        job of the task in flight is an ordinary state, not a failure: the
        session is rolled back — no version is left without its job — and the
        next submission asks again. Anything else propagates to the body, which
        logs it: the review is already out, and a failed request must not take
        it back.
        """
        submission = context.submission
        if context.redis is None:
            logger.info(
                "test_explanations_not_requested",
                submission_id=str(submission.id),
                reason="no_queue",
            )
            return
        version_id, _ = read_stored_answers(context.submission_text)
        async with context.session_factory() as session:
            published = await TestObjectRepository(session).get_version(version_id)
            review_language = context.review_language or (
                published.language if published is not None else FALLBACK_LANGUAGE
            )
            queue = ArqExplanationQueue(
                redis=context.redis, session=session, tenant_id=submission.tenant_id
            )
            try:
                await TestObjectService(session, queue).request_explanations(
                    version_id, review_language
                )
                await session.commit()
            except GenerationInProgressError:
                await session.rollback()
                logger.info(
                    "test_explanations_request_deferred",
                    submission_id=str(submission.id),
                    language=review_language,
                )


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
