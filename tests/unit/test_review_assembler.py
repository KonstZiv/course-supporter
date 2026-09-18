"""The assembler writes one review in three languages (task 04, block B2).

``TestAcceptance::test_one_fixture_assembles_in_three_languages`` is the
end-to-end proof of this task's acceptance criterion under rule P1: one
structure, three snapshots, character for character, and no model called.

**The snapshots are not regenerated.** There is no flag here that rewrites
them, and none should be added: a snapshot a test can rewrite records what the
code does, not what the code should do, and the three files are the only place
the Persian and English wording is checked by something other than trust.
Changing them is an edit to the file, made on purpose, reviewed as text.

They live beside this file, in ``snapshots/``, because the difference between
expected and actual is read by eye in three languages — two of which the reader
cannot read at all. A diff of files can be read that way; a diff of a long
string inside an assert cannot.

Persian is here for one reason: it is the only right-to-left language on the
allowed list, so it is the only one whose snapshot would change if the
assembler ever grew a dependency on writing direction.
"""

from __future__ import annotations

import doctest
import re
from pathlib import Path

import pytest

from course_supporter.homework import review_assembler
from course_supporter.homework.review_assembler import (
    MissingPhraseError,
    assemble_review,
)
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    SECTION_ORDER,
    Position,
    Reference,
    Remark,
    Reply,
    ReviewStructureV1,
    Verdict,
    Verification,
)
from course_supporter.phrasebook import phrases_for

SNAPSHOTS = Path(__file__).parent / "snapshots"

# Splits a phrase on its placeholders, so the literal parts can be looked for
# in the output whatever the placeholder was filled with.
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")

# The three the task names: the source language, the fallback, and the one
# right-to-left language on the list.
SNAPSHOT_LANGUAGES = ("ukr", "eng", "fas")


def full_review(language: str) -> ReviewStructureV1:
    """A review that uses every phrase in the dictionary.

    Deliberately maximal: all nine sections, both halves of the verification,
    all three kinds of reply, and one remark carrying each of the four kinds of
    position. A fixture that exercised less would let a phrase rot unnoticed —
    the snapshot would still match, and the unused phrase would be wrong in
    sixty files before anyone found out.

    Every phrase but one: the verdict here fails, so ``verdict.passed`` is the
    single key this cannot reach, and ``test_every_phrase_is_used`` covers it
    with the one other review it needs.

    The bodies are English in every language on purpose: they stand for what a
    model writes, and this task's claim is about the words AROUND them.
    """
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=language,
        verdict=Verdict(passed=False, why="Two of the four requirements are met."),
        fixed=[
            Remark(
                what="The loop read past the end of the list.",
                why="It crashed on an empty input.",
                todo="It is fixed — the bound is now len(items).",
                position=Position(kind="file", value="main.py"),
            )
        ],
        new_remarks=[
            Remark(
                what="The password is written in the source.",
                why="Everyone who opens the file can see it.",
                todo="Move it into an environment variable.",
                read=[
                    Reference(title="Keeping secrets", url="https://example.test/s"),
                    Reference(title="Environment", url="https://example.test/e"),
                ],
                position=Position(kind="video", value="03:41"),
            ),
            Remark(
                what="The function does three things.",
                why="A reader has to hold all three at once.",
                todo="Split it where the comments already divide it.",
                position=Position(kind="paragraph", value="7"),
            ),
        ],
        open=[
            Remark(
                what="There are still no tests for the error path.",
                why="A change there would break silently.",
                todo="Add one test for the empty input.",
                position=Position(kind="slide", value="12"),
            )
        ],
        broken=[
            Remark(
                what="Sorting no longer keeps equal items in order.",
                why="It worked in the previous submission.",
                todo="Use a stable sort.",
            )
        ],
        mentor_voice="You are close. The structure is right; the details are not yet.",
        replies=[
            Reply(
                kind="question",
                said="Why is a global variable bad here?",
                answer="Because two parts of the program can change it at once.",
            ),
            Reply(
                kind="objection",
                said="The tutorial does it this way.",
                answer="It does, and it says that is for brevity, not for production.",
            ),
            Reply(
                kind="comment",
                said="This took me longer than I expected.",
                answer="It takes everyone longer. The second time it will not.",
            ),
        ],
        verification=Verification(
            by_run=[
                "The program starts and answers on the given input.",
                "The test for the empty input fails.",
            ],
            by_reading=[
                "The names follow the convention of the course.",
                "The error path is not covered.",
            ],
        ),
        progress="Third submission in a row with no remarks about style.",
    )


class TestAcceptance:
    """Rule P1: the criterion of this task, proven end to end."""

    @pytest.mark.parametrize("language", SNAPSHOT_LANGUAGES)
    def test_one_fixture_assembles_in_three_languages(self, language: str) -> None:
        """One structure, three languages, character for character, no model.

        This is the acceptance test named in the task package: "a review is
        assembled from its structure by code, in three languages, with no
        synthesis". The snapshots are read, never written — see this module's
        docstring.
        """
        assembled = assemble_review(full_review(language))

        expected = (SNAPSHOTS / f"review_{language}.md").read_text(encoding="utf-8")
        assert assembled == expected

    def test_every_phrase_is_used(self) -> None:
        """Every one of the 25 keys reaches a reader through this assembler.

        A phrase nobody renders is a phrase nobody notices going wrong — and it
        was paid for in sixty languages. The two reviews below are all it takes
        to reach every key: the maximal one, and a passing verdict, which is
        the only thing the first cannot be at the same time.
        """
        passed = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="ukr",
            verdict=Verdict(passed=True, why="Everything asked for is there."),
        )
        rendered = assemble_review(full_review("ukr")) + assemble_review(passed)

        unused = [
            key
            for key, phrase in phrases_for("ukr").items()
            if not all(
                part in rendered for part in _PLACEHOLDER.split(phrase) if part.strip()
            )
        ]

        assert unused == []

    def test_no_provider_is_reachable_from_here(self) -> None:
        """The assembler imports nothing that could make a call.

        The snapshots would catch a call that changed the text, but not one
        that cost money and returned the same words.
        """
        source = Path(review_assembler.__file__).read_text(encoding="utf-8")

        for forbidden in ("llm", "provider", "stage_router", "httpx", "requests"):
            assert f"import {forbidden}" not in source
            assert f"from course_supporter.{forbidden}" not in source


class TestPurity:
    def test_the_same_structure_gives_the_same_bytes(self) -> None:
        review = full_review("ukr")

        assert assemble_review(review) == assemble_review(review)

    def test_two_equal_structures_give_the_same_bytes(self) -> None:
        """Not the same object: the result depends on the value, not identity."""
        assert assemble_review(full_review("eng")) == assemble_review(
            full_review("eng")
        )

    def test_the_language_is_the_structures_and_nothing_else_decides_it(self) -> None:
        """There is no argument to pass a different one through."""
        ukr = assemble_review(full_review("ukr"))
        fas = assemble_review(full_review("fas"))

        assert ukr != fas
        assert "Рецензія" in ukr
        assert "Рецензія" not in fas


class TestSections:
    def test_a_review_with_one_section_carries_only_that_section(self) -> None:
        """All nine are optional, and a silent one leaves no empty heading."""
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="eng",
            progress="Third submission in a row with no remarks about style.",
        )

        assembled = assemble_review(review)

        expected = (SNAPSHOTS / "review_one_section_eng.md").read_text(encoding="utf-8")
        assert assembled == expected
        # No heading, no separator and no blank tail from the eight that are
        # silent: the difference between "optional" and "empty".
        assert assembled.count("##") == 1

    def test_the_sections_come_in_the_ratified_order(self) -> None:
        assembled = assemble_review(full_review("eng"))
        headings = [line for line in assembled.splitlines() if line.startswith("## ")]

        assert len(headings) == len(SECTION_ORDER)
        # The verdict's heading is the verdict itself; the other eight are the
        # section names, in order.
        assert headings[0] == "## Failed"
        assert headings[-1] == "## Your progress"

    def test_a_half_empty_verification_prints_only_the_half_it_has(self) -> None:
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="eng",
            verification=Verification(by_run=["The tests pass."]),
        )

        assembled = assemble_review(review)

        assert "Checked by running" in assembled
        assert "Checked by reading" not in assembled


class TestPlaceholders:
    @pytest.mark.parametrize(
        ("kind", "value", "expected"),
        [
            ("video", "03:41", "*video, 03:41*"),
            ("slide", "12", "*slide 12*"),
            ("paragraph", "7", "*paragraph 7*"),
            ("file", "main.py", "*file main.py*"),
        ],
    )
    def test_each_kind_of_position_is_filled_with_its_value(
        self, kind: str, value: str, expected: str
    ) -> None:
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="eng",
            open=[
                Remark(
                    what="W.",
                    why="Y.",
                    todo="T.",
                    position=Position(kind=kind, value=value),  # type: ignore[arg-type]
                )
            ],
        )

        assert expected in assemble_review(review)

    def test_no_brace_survives_into_the_output(self) -> None:
        """An unfilled placeholder is the failure this is the lock for: the
        student would read the word 'number' in braces instead of a number."""
        assembled = assemble_review(full_review("fas"))

        assert "{" not in assembled
        assert "}" not in assembled


class TestMissingPhrase:
    def test_a_missing_phrase_raises_instead_of_falling_back(self) -> None:
        """The assembler has no second fallback of its own.

        Unreachable past the startup check, which refuses a phrasebook with a
        missing key. If it were reachable, a quiet substitution would give the
        student a review half in their language and nobody an error — the worst
        of the outcomes available here.
        """
        crippled = {
            key: text
            for key, text in review_assembler.phrases_for("eng").items()
            if key != "section.progress"
        }

        with pytest.raises(MissingPhraseError, match=re.escape("section.progress")):
            review_assembler._section(
                ReviewStructureV1(
                    schema_version=REVIEW_SCHEMA_VERSION,
                    language="eng",
                    progress="Good.",
                ),
                "progress",
                crippled,
                "eng",
            )


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        """Doctests are not collected by this project's pytest config, so an
        example is prose until something executes it."""
        results = doctest.testmod(review_assembler, optionflags=doctest.ELLIPSIS)

        assert results.failed == 0
        assert results.attempted > 0
