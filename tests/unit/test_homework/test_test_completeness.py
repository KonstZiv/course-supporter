"""Whether a draft is finished: every unfinished place, in reading order (task 07c).

What is pinned here:

* **Each rule names its place** — a test with no questions as a whole, an
  empty text at its question or its option, too few options and no right one
  at their question.
* **A question with no options is only short of options** — "mark one right"
  says nothing there.
* **The places come in the order the author reads the cards**, and a finished
  draft has none.
* **The refusal carries every place** the reading would list.
* **The module's examples run** — explicitly, because the gate collects no
  doctests (``DD-SP-BC``).
"""

from __future__ import annotations

import doctest

import pytest

from course_supporter.homework import test_completeness
from course_supporter.homework.test_completeness import (
    MIN_OPTIONS,
    DraftIncompleteError,
    IncompleteCode,
    IncompletePlace,
    incomplete_places,
    require_complete,
)
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
)

_EMPTY = IncompleteCode.TEST_TEXT_EMPTY
_COUNT = IncompleteCode.TEST_OPTIONS_COUNT
_NO_MARK = IncompleteCode.TEST_NO_CORRECT_OPTION


def _question(
    text: str = "Що?", *options: tuple[str, bool], explanation: str | None = None
) -> DraftQuestion:
    marks = options or (("так", True), ("ні", False))
    return DraftQuestion(
        text=text,
        options=tuple(DraftOption(text=t, correct=c) for t, c in marks),
        explanation=explanation,
    )


def _draft(*questions: DraftQuestion) -> DraftBody:
    return DraftBody(questions=questions)


class TestWhatIsUnfinished:
    def test_a_complete_draft_has_nothing_unfinished(self) -> None:
        finished = _draft(_question(), _question("А це?", ("1", False), ("2", True)))

        assert incomplete_places(finished) == ()
        require_complete(finished)

    def test_a_test_without_questions_is_unfinished_as_a_whole(self) -> None:
        assert incomplete_places(_draft()) == (
            IncompletePlace(IncompleteCode.TEST_NO_QUESTIONS),
        )

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
    def test_an_empty_text_is_named_at_its_question_or_option(self, empty: str) -> None:
        """Empty once trimmed, as the reading of a draft trims it."""
        draft = _draft(_question(empty), _question("Що?", (empty, True), ("ні", False)))

        assert incomplete_places(draft) == (
            IncompletePlace(_EMPTY, 1),
            IncompletePlace(_EMPTY, 2, 1),
        )

    def test_fewer_than_two_options_are_named_at_their_question(self) -> None:
        one = _draft(_question("Що?", ("так", True)))
        enough = _draft(
            _question("Що?", *[(f"{n}", n == 0) for n in range(MIN_OPTIONS)])
        )

        assert incomplete_places(one) == (IncompletePlace(_COUNT, 1),)
        assert incomplete_places(enough) == ()

    def test_an_unmarked_question_is_named_unless_it_has_no_options(self) -> None:
        unmarked = _draft(_question("Що?", ("так", False), ("ні", False)))
        bare = _draft(DraftQuestion(text="Що?", options=()))

        assert incomplete_places(unmarked) == (IncompletePlace(_NO_MARK, 1),)
        assert incomplete_places(bare) == (IncompletePlace(_COUNT, 1),)

    def test_the_places_come_in_reading_order(self) -> None:
        """Question by question: its text, its options' texts, their count, the mark."""
        draft = _draft(
            _question("", ("", False)),
            _question(),
            DraftQuestion(text="Що?", options=()),
        )

        assert incomplete_places(draft) == (
            IncompletePlace(_EMPTY, 1),
            IncompletePlace(_EMPTY, 1, 1),
            IncompletePlace(_COUNT, 1),
            IncompletePlace(_NO_MARK, 1),
            IncompletePlace(_COUNT, 3),
        )

    def test_a_refusal_carries_every_place(self) -> None:
        draft = _draft(_question("", ("так", False)))

        with pytest.raises(DraftIncompleteError) as refused:
            require_complete(draft)

        assert refused.value.code == "TEST_DRAFT_INCOMPLETE"
        assert refused.value.places == incomplete_places(draft)
        assert [place.to_json() for place in refused.value.places] == [
            {"code": "TEST_TEXT_EMPTY", "question": 1, "option": None},
            {"code": "TEST_OPTIONS_COUNT", "question": 1, "option": None},
            {"code": "TEST_NO_CORRECT_OPTION", "question": 1, "option": None},
        ]


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``).

    Both halves are asserted: ``failed == 0`` is also true of a module whose
    examples were all deleted.
    """
    results = doctest.testmod(test_completeness, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
