"""Scoring a test and choosing the explanation of a wrong answer (task 07).

Every rule here is ratified text — ``03-BINDING.md`` 4.4 points 4-6 and
``TASK.md`` decisions 13, 14, 20 and 23 — and each class below is one of them.

The rounding rule has exactly ONE test that depends on it: two right out of
three, against a pass mark of 67. Every other score in this file is an exact
fraction (0/4, 1/2, 3/4, 4/4), so rounding up instead of down reddens that one
test and nothing else — which is how the mutation of this block names its lock.
"""

from __future__ import annotations

import doctest

import pytest

from course_supporter.homework import test_scoring
from course_supporter.homework.test_scoring import (
    Explanation,
    ExplanationSource,
    check_question,
    choose_explanation,
    correctness,
    pass_verdict,
    reported_passed,
    score_answers,
    score_percent,
)

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"], "4": ["г"]}


class TestTheRoundingRule:
    def test_two_of_three_is_66_and_not_passed_at_67(self) -> None:
        """66.7 % shown as 66, and a pass mark of 67 is not reached.

        The one rounding-sensitive case in the file. Rounded up, the student
        would be shown 67 and told they passed with two thirds of the answers.
        """
        score = score_percent(2, 3)
        assert score == 66
        assert pass_verdict(score, 67) is False


class TestTheScore:
    def test_nothing_right_is_zero_and_everything_right_is_100(self) -> None:
        assert score_percent(0, 4) == 0
        assert score_percent(4, 4) == 100

    def test_a_test_with_no_questions_has_no_score(self) -> None:
        with pytest.raises(ValueError, match="no score"):
            score_percent(0, 0)

    def test_more_right_answers_than_questions_is_not_a_count(self) -> None:
        with pytest.raises(ValueError, match="not a count"):
            score_percent(5, 4)


class TestOneQuestion:
    def test_an_unanswered_question_is_wrong(self) -> None:
        assert check_question(None, ["а"]) is False
        assert check_question([], ["а"]) is False

    def test_an_unanswered_question_counts_against_the_score(self) -> None:
        """Three right, one not mentioned at all: wrong, not refused."""
        sheet = score_answers(_KEY, {"1": ["б"], "2": ["в"], "3": ["а"]})
        assert sheet.checks["4"] is False
        assert (sheet.correct, sheet.total, sheet.score) == (3, 4, 75)

    @pytest.mark.parametrize(
        ("given", "right"),
        [
            (["а", "в"], True),
            (["в", "а"], True),
            (["A", "в)"], True),
            (["а"], False),
            (["а", "в", "б"], False),
            (["б"], False),
        ],
        ids=["same", "reordered", "latin-and-bracket", "too-few", "too-many", "other"],
    )
    def test_several_right_labels_need_the_exact_set(
        self, given: list[str], right: bool
    ) -> None:
        """A key of two labels: all of them and nothing else is right."""
        assert check_question(given, ["а", "в"]) is right


class TestScoringAWholeKey:
    def test_every_question_of_the_key_is_checked_in_counting_order(self) -> None:
        key = {str(n): ["а"] for n in range(1, 11)}
        sheet = score_answers(key, {str(n): ["а"] for n in range(1, 11)})
        assert list(sheet.checks) == [str(n) for n in range(1, 11)]
        assert sheet.score == 100

    def test_an_answer_to_a_question_the_key_does_not_have_is_not_counted(
        self,
    ) -> None:
        sheet = score_answers({"1": ["а"], "2": ["б"]}, {"1": ["а"], "9": ["а"]})
        assert set(sheet.checks) == {"1", "2"}
        assert sheet.score == 50


class TestTheVerdict:
    def test_the_pass_mark_itself_passes(self) -> None:
        assert pass_verdict(80, 80) is True
        assert pass_verdict(79, 80) is False

    def test_no_pass_mark_is_no_verdict(self) -> None:
        assert pass_verdict(0, None) is None

    def test_a_caller_is_told_passed_when_there_is_no_pass_mark(self) -> None:
        """Decision 14: no bar was set, so nothing is held back."""
        assert reported_passed(0, None) is True
        assert reported_passed(79, 80) is False
        assert reported_passed(80, 80) is True

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (100, "correct"),
            (0, "incorrect"),
            (1, "partially_correct"),
            (99, "partially_correct"),
        ],
    )
    def test_correctness_follows_the_score(self, score: int, expected: str) -> None:
        assert correctness(score) == expected


_MODEL_REVIEW = "пояснення мовою рецензії"
_MODEL_COURSE = "пояснення мовою курсу"
_AUTHOR = "пояснення автора"


class TestChoosingTheExplanation:
    """Pre-flight table 7.3, row by row, plus the right answer that gets none."""

    def test_a_right_answer_gets_its_verdict_and_nothing_else(self) -> None:
        assert (
            choose_explanation(
                correct=True,
                author=_AUTHOR,
                doubted=False,
                in_review_language=_MODEL_REVIEW,
                in_course_language=_MODEL_COURSE,
            )
            is None
        )

    @pytest.mark.parametrize(
        ("author", "doubted", "review", "course", "expected"),
        [
            (
                _AUTHOR,
                False,
                _MODEL_REVIEW,
                _MODEL_COURSE,
                Explanation(_AUTHOR, ExplanationSource.AUTHOR, in_course_language=True),
            ),
            (
                _AUTHOR,
                True,
                _MODEL_REVIEW,
                _MODEL_COURSE,
                Explanation(_AUTHOR, ExplanationSource.AUTHOR, in_course_language=True),
            ),
            (
                None,
                True,
                _MODEL_REVIEW,
                _MODEL_COURSE,
                Explanation(None, ExplanationSource.NONE, in_course_language=False),
            ),
            (
                None,
                False,
                _MODEL_REVIEW,
                _MODEL_COURSE,
                Explanation(
                    _MODEL_REVIEW, ExplanationSource.MODEL, in_course_language=False
                ),
            ),
            (
                None,
                False,
                None,
                _MODEL_COURSE,
                Explanation(
                    _MODEL_COURSE, ExplanationSource.MODEL, in_course_language=True
                ),
            ),
            (
                None,
                False,
                None,
                None,
                Explanation(None, ExplanationSource.NONE, in_course_language=False),
            ),
        ],
        ids=[
            "author-wins",
            "author-survives-a-doubt",
            "doubt-hides-the-model",
            "model-in-review-language",
            "model-in-course-language",
            "nothing-to-show",
        ],
    )
    def test_a_wrong_answer_gets_the_row_of_the_table(
        self,
        author: str | None,
        doubted: bool,
        review: str | None,
        course: str | None,
        expected: Explanation,
    ) -> None:
        assert (
            choose_explanation(
                correct=False,
                author=author,
                doubted=doubted,
                in_review_language=review,
                in_course_language=course,
            )
            == expected
        )

    def test_a_blank_author_explanation_counts_as_absent(self) -> None:
        chosen = choose_explanation(
            correct=False,
            author="   ",
            doubted=False,
            in_review_language=_MODEL_REVIEW,
            in_course_language=_MODEL_COURSE,
        )
        assert chosen is not None
        assert chosen.source is ExplanationSource.MODEL


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``)."""
    results = doctest.testmod(test_scoring, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
