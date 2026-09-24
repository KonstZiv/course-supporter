"""A test as structure: the parser, canonical labels, canonical answers (task 07).

Three claims here are pinned to something outside this file:

* **Acceptance criterion 3.** The two reference versions stored on production
  for the polygon's test keep their digests through the canonical form. The keys
  and the full digests are those of ``07-test-submission/PROBE-REPORT.md``,
  section 8, where they were measured by calling the task-06 function on the
  production rows (2026-09-23). A canonical form that moved either digest would
  orphan a paid version — the stop condition of ``TASK.md``.
* **The parser reads the real test** — the byte-for-byte copy of
  ``06-reference/TEST-SOURCE.md`` the reference tests already pin by digest.
* **The module's examples run** — explicitly, because the gate collects no
  doctests (``DD-SP-BC``).
"""

from __future__ import annotations

import doctest
import hashlib
from pathlib import Path

import pytest

from course_supporter.homework import test_text
from course_supporter.homework.reference_key import answers_digest
from course_supporter.homework.test_text import (
    canonical_answers,
    canonical_answers_json,
    canonical_label,
    parse_test,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "reference" / "test_source.md"
_FIXTURE_SHA256 = "ed51186e7369897324997a032dd011597ef6467c4084bbc9b2960489199da72a"

_VERSION_1_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["г"],
    "5": ["в"],
}
_VERSION_1_DIGEST = "f5ae072d8b297daacdd3904453ae8e0af8441246790c797f8620afb7fd01a47f"
_VERSION_2_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["г"],
    "5": ["а"],
}
_VERSION_2_DIGEST = "0dd36e2df0059057a57a8e9ba158abfa7e3b997c92e459a7d7d030dae6b2b7aa"


@pytest.fixture(scope="module")
def test_source() -> str:
    """The acceptance test's text, verified to be the canonical file."""
    raw = _FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FIXTURE_SHA256
    return raw.decode("utf-8")


class TestPolygonDigests:
    """Acceptance criterion 3: the stored versions do not move."""

    @pytest.mark.parametrize(
        ("key", "digest"),
        [
            (_VERSION_1_KEY, _VERSION_1_DIGEST),
            (_VERSION_2_KEY, _VERSION_2_DIGEST),
        ],
        ids=["version-1", "version-2"],
    )
    def test_a_stored_version_keeps_its_digest(
        self, key: dict[str, list[str]], digest: str
    ) -> None:
        """The digest measured on production, reproduced through the new form."""
        assert canonical_answers(key) == key, (
            "the premise: the stored key is already canonical"
        )
        assert answers_digest(key) == digest


class TestLatinLabels:
    def test_latin_twins_read_as_the_cyrillic_labels(self) -> None:
        """A capital is mapped before the label is lowered, a small letter after."""
        assert [canonical_label(label) for label in ("A", "a", "B", "C", "c")] == [
            "а",
            "а",
            "в",
            "с",
            "с",
        ]

    def test_a_key_typed_in_latin_digests_as_the_cyrillic_key(self) -> None:
        """The polygon's key, with every label that has a Latin twin typed in Latin.

        ``б`` and ``г`` have no twin and stay as they are; ``в`` has one only as
        a capital, so it is typed as ``B``.
        """
        typed_in_latin = {"1": ["б"], "2": ["B"], "3": ["A"], "4": ["г"], "5": ["B"]}
        assert answers_digest(typed_in_latin) == _VERSION_1_DIGEST

    def test_a_small_b_is_not_a_twin(self) -> None:
        """No Cyrillic label looks like ``b``; a guess would turn wrong into right."""
        assert canonical_label("b") == "b"


class TestCanonicalAnswers:
    def test_spaces_and_a_trailing_bracket_or_full_stop_go(self) -> None:
        assert [canonical_label(label) for label in (" а) ", "в.", "г )")] == [
            "а",
            "в",
            "г",
        ]

    def test_one_answer_typed_four_ways_is_one_label(self) -> None:
        assert canonical_answers({"1": ["а", "А", "a", "а)"]}) == {"1": ["а"]}

    def test_question_numbers_lose_their_spaces_and_merge(self) -> None:
        assert canonical_answers({" 1 ": ["а"], "1": ["б"]}) == {"1": ["а", "б"]}

    def test_an_empty_label_is_no_answer_but_the_question_stays(self) -> None:
        assert canonical_answers({"1": [")"]}) == {"1": []}

    def test_the_stored_text_is_compact_and_unescaped(self) -> None:
        assert canonical_answers_json(_VERSION_1_KEY) == (
            '{"1":["б"],"2":["в"],"3":["а"],"4":["г"],"5":["в"]}'
        )


class TestTheParserReadsTheRealTest:
    def test_five_questions_of_four_options(self, test_source: str) -> None:
        test = parse_test(test_source)
        assert test.numbers() == ("1", "2", "3", "4", "5")
        assert all(
            [option.label for option in question.options] == ["а", "б", "в", "г"]
            for question in test.questions
        )

    def test_question_and_option_texts_are_the_authors(self, test_source: str) -> None:
        first = parse_test(test_source).questions[0]
        assert first.text == "Що таке контекстне вікно моделі?"
        assert (
            first.options[1].text == "Обсяг тексту, який модель бачить за один виклик"
        )

    def test_the_title_and_instructions_belong_to_no_question(
        self, test_source: str
    ) -> None:
        texts = [question.text for question in parse_test(test_source).questions]
        assert not any("Оберіть одну відповідь" in text for text in texts)


class TestTheLineRules:
    def test_blank_lines_between_paragraphs_are_allowed(self) -> None:
        """A Word paragraph arrives as its own block, with a blank line around it."""
        test = parse_test("1. Питання?\n\nа) так\n\nб) ні\n\n2. Ще?\n\nа) ні")
        assert [len(question.options) for question in test.questions] == [2, 1]

    def test_a_question_text_may_run_over_lines(self) -> None:
        test = parse_test("1. Перший рядок\nдругий рядок\nа) так")
        assert test.questions[0].text == "Перший рядок другий рядок"

    def test_lines_after_the_last_option_are_not_part_of_the_test(self) -> None:
        test = parse_test("1. Питання?\nа) так\nПримітка автора.\n2. Друге?\nа) ні")
        assert test.questions[0].text == "Питання?"
        assert [option.text for option in test.questions[0].options] == ["так"]

    def test_a_decimal_at_the_start_of_a_line_is_not_a_question(self) -> None:
        assert parse_test("3.14 — число пі\n1. Питання?").numbers() == ("1",)

    def test_an_indented_label_is_not_an_option(self) -> None:
        """At the very start of the line, as the author is told to write it."""
        assert parse_test("1. Питання?\n  а) з відступом").questions[0].options == ()

    def test_a_repeated_number_is_kept_but_counted_once(self) -> None:
        test = parse_test("1. Перше\n1. Знову перше\n2. Друге")
        assert len(test.questions) == 3
        assert test.numbers() == ("1", "2")


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``).

    Both halves are asserted: ``failed == 0`` is also true of a module whose
    examples were all deleted.
    """
    results = doctest.testmod(test_text, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
