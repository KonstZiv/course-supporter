"""The review structure holds its shape (mentor-rebuild task 04, block B1).

Nothing here assembles anything — that is the assembler, B2. This pins what the
data contract refuses: every refusal is a class of broken review no later stage
can then produce — a review in no language, a review that says nothing, a remark
missing half the contrasting form.
"""

from __future__ import annotations

import doctest

import pytest
from pydantic import ValidationError

from course_supporter.models import review_structure
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

_A_REMARK = Remark(what="Wrong.", why="It matters.", todo="Fix it.")
# Reached through the module: a class named Test* imported into a test module
# is something pytest tries to collect.
_AN_OPTION = review_structure.TestOption(label="b", text="The right one.")
_A_TEST = review_structure.TestSection(
    score=50,
    questions=[
        review_structure.TestQuestionResult(number="1", correct=True),
        review_structure.TestQuestionResult(
            number="2", correct=False, correct_answer=[_AN_OPTION]
        ),
    ],
    retry_offer=True,
)


def _review(**sections: object) -> ReviewStructureV1:
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION, language="ukr", **sections
    )


class TestVersion:
    def test_the_version_is_the_one_from_task_01_and_there_is_no_second(self) -> None:
        """One review, one version key, named in one place."""
        review = _review(progress="Good.")

        assert review.schema_version == REVIEW_SCHEMA_VERSION
        # Nothing else in the model carries a version of its own: a second one
        # would give two answers to "what shape is this".
        version_fields = [
            name
            for name in ReviewStructureV1.model_fields
            if "version" in name or "schema" in name
        ]
        assert version_fields == ["schema_version"]

    def test_it_is_refused_without_a_version(self) -> None:
        with pytest.raises(ValidationError, match="schema_version"):
            ReviewStructureV1(language="ukr", progress="Good.")  # type: ignore[call-arg]

    def test_it_reads_back_through_task_01s_reader(self) -> None:
        stored = _review(progress="Good.").model_dump()

        assert review_structure.VersionedReview.model_validate(
            {"schema_version": stored["schema_version"]}
        )


class TestLanguage:
    def test_a_review_without_a_language_cannot_be_built(self) -> None:
        """The pair "structure in one language, assembled in another" is the
        thing this makes unreachable: there is no second place to disagree."""
        with pytest.raises(ValidationError, match="language"):
            ReviewStructureV1(  # type: ignore[call-arg]
                schema_version=REVIEW_SCHEMA_VERSION, progress="Good."
            )

    @pytest.mark.parametrize("code", ["uk", "ukrainian", "UKR", "", "ukr "])
    def test_it_must_look_like_a_639_3_code(self, code: str) -> None:
        with pytest.raises(ValidationError, match="639-3"):
            ReviewStructureV1(
                schema_version=REVIEW_SCHEMA_VERSION, language=code, progress="Good."
            )

    def test_a_code_off_todays_allowed_list_is_still_accepted(self) -> None:
        """Deliberate: the list may change, and a stored review must stay readable.

        Membership is checked at the door, where the caller can be told; refusing
        it here would make an old review unreadable because a language was
        dropped from the whitelist years later.
        """
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION, language="epo", progress="Good."
        )

        assert review.language == "epo"


class TestSections:
    def test_the_ten_sections_are_the_ratified_order(self) -> None:
        """§2.13: verdict, test, fixed, new, open, broken, voice, replies,
        checks, progress — in that order and no other. The test's questions
        come right under the verdict and its score (task 07, decision 13)."""
        assert SECTION_ORDER == (
            "verdict",
            "test",
            "fixed",
            "new_remarks",
            "open",
            "broken",
            "mentor_voice",
            "replies",
            "verification",
            "progress",
        )

    def test_every_section_in_the_order_is_a_field(self) -> None:
        """A name in the order that is not a field would be a section that can
        never be filled — and the assembler would walk past it in silence."""
        assert set(SECTION_ORDER) <= set(ReviewStructureV1.model_fields)

    def test_every_section_field_is_in_the_order(self) -> None:
        """And the other way: a section the model can hold but the order does
        not name would be written by a stage and read by nobody."""
        not_a_section = {"schema_version", "language"}
        fields = set(ReviewStructureV1.model_fields) - not_a_section

        assert fields == set(SECTION_ORDER)

    @pytest.mark.parametrize("section", SECTION_ORDER)
    def test_one_section_alone_is_a_valid_review(self, section: str) -> None:
        """Each of the ten, on its own. All of them are optional."""
        only: dict[str, object] = {
            "verdict": Verdict(passed=True, why="Good work."),
            "test": _A_TEST,
            "fixed": [_A_REMARK],
            "new_remarks": [_A_REMARK],
            "open": [_A_REMARK],
            "broken": [_A_REMARK],
            "mentor_voice": "A word from me.",
            "replies": [Reply(kind="question", said="Why?", answer="Because.")],
            "verification": Verification(by_run=["The tests pass."]),
            "progress": "Getting better.",
        }

        review = _review(**{section: only[section]})

        assert getattr(review, section) == only[section]

    def test_a_review_with_no_sections_at_all_is_refused(self) -> None:
        """Not an edge case: "here is your review" followed by nothing is a
        defect, and this is the one place no stage can route around."""
        with pytest.raises(ValidationError, match="says nothing to the student"):
            _review()

    def test_empty_collections_do_not_count_as_sections(self) -> None:
        with pytest.raises(ValidationError, match="says nothing to the student"):
            _review(fixed=[], replies=[], new_remarks=[])

    def test_a_verification_that_verifies_nothing_is_refused(self) -> None:
        """Otherwise a review whose only section is an empty verification
        passes the check above and still assembles to nothing."""
        with pytest.raises(ValidationError, match="what was run or what was read"):
            Verification()


class TestRemark:
    @pytest.mark.parametrize("missing", ["what", "why", "todo"])
    def test_the_contrasting_form_is_not_optional(self, missing: str) -> None:
        """A remark without its "why" leaves the student guessing whether it
        matters; without its "todo", with a verdict instead of a lesson."""
        parts = {"what": "Wrong.", "why": "It matters.", "todo": "Fix it."}
        del parts[missing]

        with pytest.raises(ValidationError, match=missing):
            Remark(**parts)  # type: ignore[arg-type]

    @pytest.mark.parametrize("blank", ["what", "why", "todo"])
    def test_an_empty_part_is_the_same_as_a_missing_one(self, blank: str) -> None:
        parts = {"what": "Wrong.", "why": "It matters.", "todo": "Fix it."}
        parts[blank] = ""

        with pytest.raises(ValidationError, match=blank):
            Remark(**parts)  # type: ignore[arg-type]

    def test_reading_and_position_are_optional(self) -> None:
        assert _A_REMARK.read == []
        assert _A_REMARK.position is None

    def test_a_reference_carries_both_a_title_and_a_destination(self) -> None:
        """Code resolves the identifier before it lands here (§2.12), so the
        assembler never meets a bare id."""
        with pytest.raises(ValidationError, match="url"):
            Reference(title="The docs")  # type: ignore[call-arg]


class TestParts:
    @pytest.mark.parametrize("kind", ["video", "slide", "paragraph", "file"])
    def test_the_four_kinds_of_position(self, kind: str) -> None:
        assert Position(kind=kind, value="12").kind == kind  # type: ignore[arg-type]

    def test_a_fifth_kind_of_position_is_refused(self) -> None:
        """A kind with no phrase behind it would assemble to a blank."""
        with pytest.raises(ValidationError, match="kind"):
            Position(kind="chapter", value="2")  # type: ignore[arg-type]

    @pytest.mark.parametrize("kind", ["question", "objection", "comment"])
    def test_the_three_kinds_of_reply(self, kind: str) -> None:
        reply = Reply(kind=kind, said="...", answer="...")  # type: ignore[arg-type]

        assert reply.kind == kind

    def test_what_the_student_said_is_kept_beside_the_answer(self) -> None:
        """So a review read a year later still makes sense on its own."""
        with pytest.raises(ValidationError, match="said"):
            Reply(kind="question", answer="Because.")  # type: ignore[call-arg]

    def test_a_verdict_says_why_unless_a_test_score_does(self) -> None:
        """On both outcomes: a pass with no reason teaches as little as a
        failure with none. The rule sits on the review since task 07: a test's
        verdict leaves its reason to the score (decision 20), which only the
        review, holding both, can tell apart."""
        with pytest.raises(ValidationError, match="says why"):
            _review(verdict=Verdict(passed=True))


class TestTheTestSection:
    """A test's result, question by question (task 07, decisions 13 and 20)."""

    def test_a_tests_verdict_may_leave_its_reason_to_the_score(self) -> None:
        review = _review(verdict=Verdict(passed=False), test=_A_TEST)

        assert review.verdict is not None
        assert review.verdict.why is None

    def test_a_right_answer_gets_its_verdict_and_nothing_else(self) -> None:
        """03-BINDING 4.4, point 5: nothing to explain on a right answer."""
        with pytest.raises(ValidationError, match="nothing else"):
            review_structure.TestQuestionResult(
                number="1", correct=True, correct_answer=[_AN_OPTION]
            )
        with pytest.raises(ValidationError, match="nothing else"):
            review_structure.TestQuestionResult(
                number="1", correct=True, explanation="Because."
            )

    def test_a_wrong_answer_shows_the_correct_one(self) -> None:
        with pytest.raises(ValidationError, match="shows the correct answer"):
            review_structure.TestQuestionResult(number="2", correct=False)
        with pytest.raises(ValidationError, match="shows the correct answer"):
            review_structure.TestQuestionResult(
                number="2", correct=False, correct_answer=[]
            )

    def test_a_wrong_answer_may_have_no_explanation(self) -> None:
        """4.4, point 8: none written yet, or one the model doubted."""
        question = review_structure.TestQuestionResult(
            number="2", correct=False, correct_answer=[_AN_OPTION]
        )

        assert question.explanation is None

    @pytest.mark.parametrize("score", [-1, 101])
    def test_the_score_is_a_whole_percent(self, score: int) -> None:
        with pytest.raises(ValidationError, match="score"):
            review_structure.TestSection(score=score, questions=_A_TEST.questions)

    def test_a_test_section_has_questions(self) -> None:
        with pytest.raises(ValidationError, match="questions"):
            review_structure.TestSection(score=0, questions=[])

    def test_it_survives_a_round_trip_through_storage(self) -> None:
        original = _review(verdict=Verdict(passed=False), test=_A_TEST)

        assert ReviewStructureV1.model_validate(original.model_dump()) == original


class TestShape:
    def test_an_unknown_field_is_refused(self) -> None:
        """Inherited from VersionedReview's extra="forbid": a stage that
        invents a section would otherwise have it silently dropped on read."""
        with pytest.raises(ValidationError, match="score"):
            _review(progress="Good.", score=90)

    def test_it_survives_a_round_trip_through_storage(self) -> None:
        """The structure is stored as JSONB and read back by a later reader;
        anything that does not survive that is not a contract."""
        original = _review(
            verdict=Verdict(passed=False, why="Not yet."),
            new_remarks=[
                Remark(
                    what="Wrong.",
                    why="It matters.",
                    todo="Fix it.",
                    read=[Reference(title="The docs", url="https://example.test/d")],
                    position=Position(kind="video", value="03:41"),
                )
            ],
        )

        assert ReviewStructureV1.model_validate(original.model_dump()) == original


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        """The project's doctests are not collected by the gate, so this runs
        this module's. A docstring that lies does not turn the gate red on its
        own — that lesson cost a wrong claim in a shipped interface before."""
        results = doctest.testmod(
            review_structure,
            optionflags=doctest.ELLIPSIS | doctest.IGNORE_EXCEPTION_DETAIL,
        )

        assert results.failed == 0
        assert results.attempted > 0
