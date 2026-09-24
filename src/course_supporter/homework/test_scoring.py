"""How a test is scored, and which explanation a wrong answer gets (task 07).

Purpose:
    Everything a test review decides that is not text: whether an answer is
    right, what the score is, whether the test is passed, what the caller-facing
    ``correctness`` is, and which explanation — the author's, the model's in one
    of two languages, or none — stands under a wrong answer. Pure functions: no
    database, no network, no settings, no clock. The same inputs give the same
    result, so the submission path calls them today and an answer checked the
    moment it is given (third echelon) will call the very same ones.

Interface:
    :func:`check_question` — one question, right or not.
    :func:`score_answers` — every question of a key against a set of answers.
    :func:`score_percent` — the score from the two counts.
    :func:`pass_verdict` — passed, not passed, or no verdict (no pass mark).
    :func:`reported_passed` — the ``passed`` a caller is sent.
    :func:`correctness` — the caller-facing coarse verdict from the score.
    :func:`choose_explanation` — what stands under a wrong answer.

The rules (``03-BINDING.md`` 4.4, points 4-6; ``TASK.md`` decisions 13, 14, 20,
23):

* A question is right when the SET of its canonical labels equals the key's —
  one label or several, nothing more and nothing less. A question left without
  an answer is wrong, not refused.
* The score is the share of questions answered right, in whole percent,
  rounded DOWN: the number a student is shown never overstates. The pass mark is
  compared with that same whole number, so the score shown and the verdict
  given can never disagree.
* No pass mark means no verdict in the review — and ``passed = true`` for a
  caller that needs a boolean: the author set no bar to fail.

The examples spell labels in ASCII: a label is an opaque token to scoring, and
the canonical form (``homework/test_text.py``) turns a Latin ``x`` and a Latin
``X`` into the same Cyrillic letter.

>>> sheet = score_answers({"1": ["x"], "2": ["y"]}, {"1": ["X"], "2": ["a"]})
>>> sheet.checks, sheet.score
({'1': True, '2': False}, 50)
>>> pass_verdict(50, None), reported_passed(50, None), correctness(50)
(None, True, 'partially_correct')

Replacing a rule:
    The formula of the score lives in :func:`score_percent` and nowhere else; a
    weighted score is a new function beside it, taking the weights, and the
    caller picks one. The choice of explanation is one table in
    :func:`choose_explanation`, read from top to bottom.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from course_supporter.homework.test_text import canonical_label

__all__ = [
    "Correctness",
    "Explanation",
    "ExplanationSource",
    "ScoredAnswers",
    "check_question",
    "choose_explanation",
    "correctness",
    "pass_verdict",
    "reported_passed",
    "score_answers",
    "score_percent",
]

Correctness = Literal["correct", "partially_correct", "incorrect"]
"""The caller-facing coarse verdict — the webhook contract's own three values."""


def check_question(given: Iterable[str] | None, expected: Iterable[str]) -> bool:
    """Is this answer right: the same set of canonical labels as the key?

    Both sides are reduced to canonical labels first, so a Latin ``a`` answers
    its Cyrillic twin and ``a)`` answers ``a``. An answer with no label — or no
    answer at all — is wrong, even against a key that is somehow empty.

    >>> check_question(["a", "c"], ["c", "a"]), check_question(["a"], ["a", "c"])
    (True, False)
    >>> check_question(None, ["a"])
    False
    """
    given_labels = {canonical_label(label) for label in given or ()} - {""}
    expected_labels = {canonical_label(label) for label in expected} - {""}
    return bool(given_labels) and given_labels == expected_labels


@dataclass(frozen=True, slots=True)
class ScoredAnswers:
    """Every question of a key, checked: right or wrong, the counts and the score.

    ``checks`` is keyed by question number in the order a reader counts
    (``10`` after ``9``). Answers to questions the key does not have are not
    counted; a structure that carries them is refused at the door, before a
    submission exists (``TASK.md`` decision 21).
    """

    checks: dict[str, bool]
    correct: int
    total: int
    score: int


def score_answers(
    key: Mapping[str, Iterable[str]], answers: Mapping[str, Iterable[str]]
) -> ScoredAnswers:
    """Check every question of the key against the answers, and score them.

    Question numbers are compared as the key writes them, after the spaces
    around them go. A question of the key the answers do not mention is wrong.
    """
    given = {number.strip(): labels for number, labels in answers.items()}
    checks = {
        number: check_question(given.get(number.strip()), labels)
        for number, labels in sorted(
            key.items(), key=lambda item: _count_order(item[0])
        )
    }
    correct = sum(checks.values())
    total = len(checks)
    return ScoredAnswers(
        checks=checks,
        correct=correct,
        total=total,
        score=score_percent(correct, total),
    )


def score_percent(correct: int, total: int) -> int:
    """The share of questions answered right, in whole percent, rounded down.

    Rounded down so the score never overstates: two of three is 66, not 67.

    Raises:
        ValueError: ``total`` is not positive, or ``correct`` is outside
            ``0..total`` — a test with no questions has no score.

    >>> score_percent(0, 5), score_percent(4, 5), score_percent(5, 5)
    (0, 80, 100)
    """
    if total <= 0:
        msg = f"a test with {total} questions has no score"
        raise ValueError(msg)
    if not 0 <= correct <= total:
        msg = f"{correct} right answers out of {total} questions is not a count"
        raise ValueError(msg)
    return (100 * correct) // total


def pass_verdict(score: int, pass_threshold: int | None) -> bool | None:
    """Passed or not against the author's pass mark; ``None`` when there is none.

    The mark is compared with the whole-percent score, the number the student
    is shown — never with the exact fraction behind it.
    """
    if pass_threshold is None:
        return None
    return score >= pass_threshold


def reported_passed(score: int, pass_threshold: int | None) -> bool:
    """The ``passed`` a caller is sent: the verdict, or ``True`` with no pass mark.

    A caller may gate what comes next on this boolean; an author who set no
    pass mark set no bar, so nothing may be held back on a test's account
    (``TASK.md`` decision 14).
    """
    verdict = pass_verdict(score, pass_threshold)
    return True if verdict is None else verdict


def correctness(score: int) -> Correctness:
    """The coarse verdict from the score: all right, none right, or in between.

    >>> correctness(100), correctness(0), correctness(1)
    ('correct', 'incorrect', 'partially_correct')
    """
    if score >= 100:
        return "correct"
    if score <= 0:
        return "incorrect"
    return "partially_correct"


class ExplanationSource(StrEnum):
    """Whose words stand under a wrong answer."""

    AUTHOR = "author"
    MODEL = "model"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Explanation:
    """What stands under a wrong answer, and in which language it was written.

    ``text`` is ``None`` for :attr:`ExplanationSource.NONE`: the review then says
    there is no explanation for this question, and promises none.
    ``in_course_language`` is true when the text is written in the course
    language — the author's always, the model's when it came from the version
    written in the course language. Whether that deserves the line "given in the
    course language" depends on the review's own language, which only the
    caller knows.
    """

    text: str | None
    source: ExplanationSource
    in_course_language: bool


def choose_explanation(
    *,
    correct: bool,
    author: str | None,
    doubted: bool,
    in_review_language: str | None,
    in_course_language: str | None,
) -> Explanation | None:
    """Which explanation a question gets — one table, read top to bottom.

    A right answer gets its verdict and nothing else: ``None``. For a wrong one
    (``TASK.md`` decisions 13, 20; pre-flight table 7.3):

    ==========  =======  ========================  ======================
    author's    doubted  model's, review language  what the student sees
    ==========  =======  ========================  ======================
    present     any      any                       the author's
    absent      yes      any                       none
    absent      no       present                   the model's
    absent      no       absent                    the model's in the
                                                   course language, or none
    ==========  =======  ========================  ======================

    A doubt hides only the MODEL's words: the model has said it is not sure the
    key is right, so its reasoning for the key is not shown — the author's own
    words are, because the author owns the key. A blank text counts as absent.

    >>> choose_explanation(correct=True, author="because", doubted=False,
    ...                    in_review_language=None, in_course_language=None) is None
    True
    >>> hidden = choose_explanation(correct=False, author=None, doubted=True,
    ...                             in_review_language="because",
    ...                             in_course_language="because")
    >>> hidden.source, hidden.text
    (<ExplanationSource.NONE: 'none'>, None)
    """
    if correct:
        return None
    if _present(author):
        return Explanation(
            text=author, source=ExplanationSource.AUTHOR, in_course_language=True
        )
    if doubted:
        return _NO_EXPLANATION
    if _present(in_review_language):
        return Explanation(
            text=in_review_language,
            source=ExplanationSource.MODEL,
            in_course_language=False,
        )
    if _present(in_course_language):
        return Explanation(
            text=in_course_language,
            source=ExplanationSource.MODEL,
            in_course_language=True,
        )
    return _NO_EXPLANATION


_NO_EXPLANATION = Explanation(
    text=None, source=ExplanationSource.NONE, in_course_language=False
)


def _present(text: str | None) -> bool:
    return bool(text and text.strip())


def _count_order(number: str) -> tuple[int, int, str]:
    """Sort question numbers the way a reader counts them (``10`` after ``9``)."""
    stripped = number.strip()
    return (0, int(stripped), "") if stripped.isdigit() else (1, 0, stripped)
