"""A test's review as a structure — the pure half of the builder (task 07, Д1).

Every rule here is the structure a student reads: the verdict and the score,
the correct option as the student saw it, which explanation stands under a
wrong answer, and when the review says its explanations are in the course
language. No database: the builder's reads are the integration tests'.
"""

from __future__ import annotations

from course_supporter.homework import test_result
from course_supporter.homework.reference_service import ExplanationsView
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.test_text import parse_test
from course_supporter.models import review_structure
from course_supporter.models.review_structure import ReviewStructureV1

_TEXT = (
    "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nа) так\nв) ні\n\n3. Третє?\nа) так\nб) ні"
)
_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_ONE_WRONG: dict[str, list[str]] = {"1": ["б"], "2": ["а"], "3": ["а"]}
_COURSE_LINE = "Пояснення подано мовою курсу."


def _view(language: str, **fields: object) -> ExplanationsView:
    return ExplanationsView(language=language, answers=_KEY, **fields)  # type: ignore[arg-type]


def _build(
    answers: dict[str, list[str]],
    *,
    course: ExplanationsView,
    review: ExplanationsView | None = None,
) -> ReviewStructureV1:
    structure, _ = test_result.build_test_review(
        student_answers=answers,
        test=parse_test(_TEXT),
        course=course,
        review=course if review is None else review,
    )
    return structure


def _question(
    structure: ReviewStructureV1, number: str
) -> review_structure.TestQuestionResult:
    assert structure.test is not None
    return next(q for q in structure.test.questions if q.number == number)


class TestTheScoreAndTheVerdict:
    def test_a_pass_mark_met_is_a_verdict_and_no_offer_to_retry(self) -> None:
        structure, score = test_result.build_test_review(
            student_answers=_ONE_WRONG,
            test=parse_test(_TEXT),
            course=_view("ukr", pass_threshold=60),
            review=_view("ukr", pass_threshold=60),
        )

        assert score == 66
        assert structure.verdict is not None
        assert structure.verdict.passed is True
        assert structure.verdict.why is None, "the score is the reason (decision 20)"
        assert structure.test is not None
        assert structure.test.retry_offer is False

    def test_a_pass_mark_missed_ends_with_the_offer(self) -> None:
        structure = _build(_ONE_WRONG, course=_view("ukr", pass_threshold=67))

        assert structure.verdict is not None
        assert structure.verdict.passed is False
        assert structure.test is not None
        assert structure.test.retry_offer is True

    def test_no_pass_mark_is_no_verdict_and_no_offer(self) -> None:
        structure = _build(_ONE_WRONG, course=_view("ukr"))

        assert structure.verdict is None
        assert structure.test is not None
        assert structure.test.retry_offer is False


class TestAWrongAnswer:
    def test_it_shows_the_correct_option_as_the_student_saw_it(self) -> None:
        """The test's own label and the option's text, found by canonical label."""
        structure = _build(_ONE_WRONG, course=_view("ukr"))

        wrong = _question(structure, "2")
        assert wrong.correct is False
        answer = wrong.correct_answer
        assert [(o.label, o.text) for o in answer] == [("в", "ні")]

    def test_a_right_answer_is_its_verdict_alone(self) -> None:
        right = _question(_build(_ONE_WRONG, course=_view("ukr")), "1")

        assert right.correct is True
        assert right.correct_answer is None
        assert right.explanation is None

    def test_the_models_explanation_stands_under_it(self) -> None:
        course = _view("ukr", model={"2": "Модель пояснює друге."})

        assert _question(_build(_ONE_WRONG, course=course), "2").explanation == (
            "Модель пояснює друге."
        )

    def test_a_doubt_hides_the_models_words_but_not_the_authors(self) -> None:
        doubted = _view("ukr", model={"2": "Модель."}, doubts={"2": True})
        with_author = _view(
            "ukr", model={"2": "Модель."}, doubts={"2": True}, author={"2": "Автор."}
        )

        assert _question(_build(_ONE_WRONG, course=doubted), "2").explanation is None
        assert (
            _question(_build(_ONE_WRONG, course=with_author), "2").explanation
            == "Автор."
        )

    def test_a_doubt_in_the_review_language_hides_them_too(self) -> None:
        course = _view("ukr", model={"2": "Курс."})
        review = _view("eng", model={"2": "Review."}, doubts={"2": True})

        assert (
            _question(_build(_ONE_WRONG, course=course, review=review), "2").explanation
            is None
        )


class TestTheLineAboutTheCourseLanguage:
    """Said only when the review is in another language and shows one of them."""

    def test_a_review_in_the_course_language_never_says_it(self) -> None:
        """Even over an author's explanation, which is always in the course
        language: the student reads that language anyway (the reminder for Д1)."""
        course = _view("ukr", author={"2": "Автор пояснює."})

        structure = _build(_ONE_WRONG, course=course)

        assert structure.test is not None
        assert structure.test.explanations_in_course_language is False
        assert _COURSE_LINE not in assemble_review(structure)

    def test_a_review_in_another_language_says_it_over_a_course_explanation(
        self,
    ) -> None:
        course = _view("ukr", model={"2": "Пояснення мовою курсу."})
        review = _view("eng")

        structure = _build(_ONE_WRONG, course=course, review=review)

        assert structure.language == "eng"
        assert structure.test is not None
        assert structure.test.explanations_in_course_language is True
        assert _question(structure, "2").explanation == "Пояснення мовою курсу."

    def test_nor_when_the_review_language_has_its_own(self) -> None:
        course = _view("ukr", model={"2": "Курс."})
        review = _view("eng", model={"2": "In English."})

        structure = _build(_ONE_WRONG, course=course, review=review)

        assert structure.test is not None
        assert structure.test.explanations_in_course_language is False
        assert _question(structure, "2").explanation == "In English."

    def test_nor_when_nothing_is_shown(self) -> None:
        structure = _build(_ONE_WRONG, course=_view("ukr"), review=_view("eng"))

        assert structure.test is not None
        assert structure.test.explanations_in_course_language is False
