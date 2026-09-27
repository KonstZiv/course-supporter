"""Whether a test's draft is finished — asked before it is published (task 07c).

Purpose:
    From task 07c a draft is saved in any state (decision 11): with no
    questions yet, with a question that has no options or one, with no option
    marked right, with a text left empty. What a student could not answer, or
    could not be scored on, is not published — so a publication asks here
    first, and the draft's reading shows the author the same list, place by
    place, before anything is tried.

    The rules of the format — types, upper limits, the Stage 1 screens — stay
    where a body enters (:mod:`course_supporter.homework.test_yaml`): a draft
    that reaches this module is finished or not, never malformed.

Interface:
    :func:`incomplete_places` — every unfinished place of a draft, in the
        order the author reads it; empty for a finished draft.
    :func:`require_complete` — the same list as a refusal.
    :class:`IncompleteCode` and :class:`IncompletePlace` — what an entry says.
    :class:`DraftIncompleteError` — the refusal, with its code and its places.
    :data:`MIN_OPTIONS` — the fewest options a finished question has.

The rules, question by question in reading order — its text, its options'
texts, how many options, whether one is marked right:

    >>> from course_supporter.homework.test_object import DraftOption, DraftQuestion
    >>> unfinished = DraftBody(
    ...     questions=(
    ...         DraftQuestion(
    ...             text="", options=(DraftOption(text="так", correct=False),)
    ...         ),
    ...         DraftQuestion(text="Що?", options=()),
    ...     )
    ... )
    >>> for place in incomplete_places(unfinished):
    ...     print(place.code, place.question, place.option)
    TEST_TEXT_EMPTY 1 None
    TEST_OPTIONS_COUNT 1 None
    TEST_NO_CORRECT_OPTION 1 None
    TEST_OPTIONS_COUNT 2 None

A question with no options is only short of options: "mark one right" says
nothing there. A test with no questions is unfinished as a whole:

    >>> [str(place.code) for place in incomplete_places(DraftBody(questions=()))]
    ['TEST_NO_QUESTIONS']

Extending:
    A new rule is a code here and a check in :func:`incomplete_places`, at
    the place of the card it belongs to — the author's interface shows each
    entry where the code and the place say.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from course_supporter.homework.test_object import DraftBody

__all__ = [
    "MIN_OPTIONS",
    "DraftIncompleteError",
    "IncompleteCode",
    "IncompletePlace",
    "incomplete_places",
    "require_complete",
]

MIN_OPTIONS: Final[int] = 2
"""The fewest options a finished question has: one option is no choice."""


class IncompleteCode(StrEnum):
    """What is unfinished at a place — one code per rule."""

    TEST_NO_QUESTIONS = "TEST_NO_QUESTIONS"
    TEST_TEXT_EMPTY = "TEST_TEXT_EMPTY"
    TEST_OPTIONS_COUNT = "TEST_OPTIONS_COUNT"
    TEST_NO_CORRECT_OPTION = "TEST_NO_CORRECT_OPTION"


@dataclass(frozen=True, slots=True)
class IncompletePlace:
    """An unfinished place: its code, and the question and the option it is at.

    Positions count from 1 in the draft as saved. ``option`` is set only for
    an option's own text; ``question`` is ``None`` only for a test with no
    questions.
    """

    code: IncompleteCode
    question: int | None = None
    option: int | None = None

    def to_json(self) -> dict[str, str | int | None]:
        """An entry of ``incomplete`` — in the draft's reading and in a refusal."""
        return {
            "code": self.code.value,
            "question": self.question,
            "option": self.option,
        }


class DraftIncompleteError(Exception):
    """A draft that is not finished yet was asked to be published."""

    code: ClassVar[str] = "TEST_DRAFT_INCOMPLETE"

    def __init__(self, places: tuple[IncompletePlace, ...]) -> None:
        super().__init__(f"{self.code}: {len(places)} unfinished places")
        self.places = places


def incomplete_places(draft: DraftBody) -> tuple[IncompletePlace, ...]:
    """Every unfinished place of ``draft``, in the order the author reads it.

    A text is empty when nothing is left of it once trimmed — the reading of
    a draft trims its texts, so a draft built by hand is judged the same way.
    """
    if not draft.questions:
        return (IncompletePlace(IncompleteCode.TEST_NO_QUESTIONS),)
    places: list[IncompletePlace] = []
    for number, question in enumerate(draft.questions, start=1):
        if not question.text.strip():
            places.append(IncompletePlace(IncompleteCode.TEST_TEXT_EMPTY, number))
        places.extend(
            IncompletePlace(IncompleteCode.TEST_TEXT_EMPTY, number, position)
            for position, option in enumerate(question.options, start=1)
            if not option.text.strip()
        )
        if len(question.options) < MIN_OPTIONS:
            places.append(IncompletePlace(IncompleteCode.TEST_OPTIONS_COUNT, number))
        if question.options and not any(option.correct for option in question.options):
            places.append(
                IncompletePlace(IncompleteCode.TEST_NO_CORRECT_OPTION, number)
            )
    return tuple(places)


def require_complete(draft: DraftBody) -> None:
    """Refuse an unfinished draft with every place it is unfinished at.

    Raises:
        DraftIncompleteError: the draft has an unfinished place.
    """
    places = incomplete_places(draft)
    if places:
        raise DraftIncompleteError(places)
