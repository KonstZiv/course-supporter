"""The answer-key core: question numbers and the answer digest (task 06).

Two claims here are about the REAL world rather than about a string literal,
and both are pinned to something outside this file:

* the parser is run against the very test the live acceptance will upload — a
  byte-for-byte copy of ``06-reference/TEST-SOURCE.md``, checked by digest, so
  a reformatting of that file cannot quietly stop meaning what the tests here
  say it means;
* the truncation detector is run against text that ``stitch_task_text`` really
  truncated, not against a hand-typed marker, so a change to either side shows
  up as a red test instead of as a key accepted against half a task.

The module's docstring examples are executed here explicitly. Doctests are not
collected by the gate (``--doctest-modules`` is enabled nowhere — ``DD-SP-BC``),
so without this test they would be prose in the shape of code.
"""

from __future__ import annotations

import doctest
import hashlib
from pathlib import Path

import pytest

from course_supporter.homework import reference_key
from course_supporter.homework.reference_key import (
    answers_digest,
    compare_to_questions,
    parse_question_numbers,
)
from course_supporter.homework.task_text import stitch_task_text

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "reference" / "test_source.md"
_FIXTURE_SHA256 = "ed51186e7369897324997a032dd011597ef6467c4084bbc9b2960489199da72a"
"""Digest of ``06-reference/TEST-SOURCE.md`` in the canon at ratification.

The fixture is a copy, and a copy drifts. Pinning the digest turns that drift
into a failing test with an obvious cause, instead of into a passing test about
a file nobody compares any more.
"""

_AUTHOR_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["г"],
    "5": ["в"],
}
"""The key the live acceptance will send — ``TASK.md``, acceptance criterion 7."""


@pytest.fixture(scope="module")
def test_source() -> str:
    """The acceptance test's text, verified to be the canonical file."""
    raw = _FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == _FIXTURE_SHA256, (
        f"tests/fixtures/reference/test_source.md is no longer the canonical "
        f"TEST-SOURCE.md (sha256 {digest})"
    )
    return raw.decode("utf-8")


class TestTheParserReadsTheRealTest:
    def test_the_acceptance_test_yields_exactly_five_questions(
        self, test_source: str
    ) -> None:
        """The file the operator will upload asks 1 through 5, and nothing else."""
        found = parse_question_numbers(test_source)
        assert found.numbers == ("1", "2", "3", "4", "5")
        assert found.truncated is False
        assert bool(found) is True

    def test_the_same_text_stitched_from_segments_reads_the_same(
        self, test_source: str
    ) -> None:
        """Ingestion cuts the text into segments; stitching adds a blank line.

        This is the shape the mentor pipeline really hands over, and the one
        place an index-based parser would go wrong: every segment boundary gains
        a ``\\n\\n`` that is not in the authored file.
        """
        head, tail = test_source.split("3. Навіщо", maxsplit=1)
        stitched = stitch_task_text([head, "3. Навіщо" + tail])
        assert stitched != test_source, "the stitch must really differ from the file"
        assert parse_question_numbers(stitched).numbers == ("1", "2", "3", "4", "5")

    def test_a_number_inside_a_line_is_not_a_question(self) -> None:
        """Only a line that STARTS with the number counts."""
        assert parse_question_numbers("see item 1. of the manual").numbers == ()
        assert parse_question_numbers("  1. indented").numbers == ()
        assert parse_question_numbers("x 2. tail\n3. real").numbers == ("3",)

    def test_a_text_without_numbered_questions_is_empty_not_an_error(self) -> None:
        """The "no question numbers" refusal has its own, non-exceptional shape."""
        found = parse_question_numbers("# Тест\n\nа) перша\nб) друга")
        assert found.numbers == ()
        assert found.truncated is False
        assert bool(found) is False

    def test_a_repeated_number_is_counted_once_in_order(self) -> None:
        assert parse_question_numbers("2. b\n1. a\n2. b again").numbers == ("2", "1")


class TestTruncationIsItsOwnAnswer:
    def test_a_genuinely_truncated_stitch_is_detected(self) -> None:
        """The marker is produced by ``stitch_task_text``, not typed by hand.

        A hand-typed marker would prove only that the detector matches the
        string this file contains. Here the budget really is exceeded, so the
        two modules are checked against each other.
        """
        segments = ["1. Перше питання" + "x" * 500, "5. П'яте питання"]
        assert len(segments[0].encode()) + 2 + len(segments[1].encode()) > 540, (
            "the fixture must really exceed the budget below"
        )
        stitched = stitch_task_text(segments, max_bytes=540)
        found = parse_question_numbers(stitched)

        assert found.truncated is True, "the over-budget stitch must be flagged"
        assert "5" not in found.numbers, "the dropped segment's question is gone"
        assert bool(found) is False, "a truncated text is not usable"

    def test_an_intact_stitch_is_not_flagged(self) -> None:
        stitched = stitch_task_text(["1. Перше", "2. Друге"])
        assert parse_question_numbers(stitched).truncated is False


class TestTheDigestIsCanonical:
    def test_question_order_does_not_change_the_digest(self) -> None:
        shuffled = {"5": ["в"], "1": ["б"], "4": ["г"], "2": ["в"], "3": ["а"]}
        assert answers_digest(shuffled) == answers_digest(_AUTHOR_KEY)

    def test_label_order_within_a_question_does_not_change_the_digest(self) -> None:
        assert answers_digest({"1": ["а", "б"]}) == answers_digest({"1": ["б", "а"]})

    def test_a_changed_answer_changes_the_digest(self) -> None:
        changed = dict(_AUTHOR_KEY) | {"3": ["б"]}
        assert answers_digest(changed) != answers_digest(_AUTHOR_KEY)

    def test_an_added_or_removed_question_changes_the_digest(self) -> None:
        assert answers_digest(_AUTHOR_KEY | {"6": ["а"]}) != answers_digest(_AUTHOR_KEY)
        without_five = {k: v for k, v in _AUTHOR_KEY.items() if k != "5"}
        assert answers_digest(without_five) != answers_digest(_AUTHOR_KEY)

    def test_the_digest_is_a_sha256_hex_string(self) -> None:
        digest = answers_digest(_AUTHOR_KEY)
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_author_explanations_cannot_enter_the_digest(self) -> None:
        """Not by policy — by signature: the function takes answers and nothing else.

        The rule (ratified 2026-09-19) is that editing an author's explanation
        must not buy a fresh generation. A test that passed explanations in and
        asserted they were ignored would be testing a branch that does not
        exist; what holds the rule is that there is nowhere to put them.
        """
        import inspect

        parameters = inspect.signature(answers_digest).parameters
        assert list(parameters) == ["answers"]


class TestComparingAKeyToItsQuestions:
    def test_a_complete_key_has_no_mismatch(self, test_source: str) -> None:
        found = parse_question_numbers(test_source)
        assert not compare_to_questions(_AUTHOR_KEY, found)

    def test_a_missing_and_an_unknown_are_reported_together(
        self, test_source: str
    ) -> None:
        found = parse_question_numbers(test_source)
        key = {k: v for k, v in _AUTHOR_KEY.items() if k != "3"} | {"9": ["а"]}
        mismatch = compare_to_questions(key, found)
        assert mismatch.missing == ("3",)
        assert mismatch.unknown == ("9",)
        assert bool(mismatch) is True

    def test_numbers_are_reported_in_counting_order(self) -> None:
        """``10`` comes after ``9``, the way a reader counts, not the way bytes sort."""
        questions = parse_question_numbers("\n".join(f"{n}. q" for n in range(1, 12)))
        mismatch = compare_to_questions({"1": ["а"]}, questions)
        assert mismatch.missing[:3] == ("2", "3", "4")
        assert mismatch.missing[-1] == "11"


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``).

    ``--doctest-modules`` is enabled nowhere in this project, so an example in a
    docstring never runs and never fails. Task 04 established this shape: the
    module's own test executes them and asserts both that they passed AND that
    there were some, because ``failed == 0`` is also true of a module with no
    examples at all.
    """
    results = doctest.testmod(reference_key, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
