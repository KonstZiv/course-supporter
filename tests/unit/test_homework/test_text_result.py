"""A text task's review, laid out from its verdicts and their explanation (09b, K6).

:func:`build_text_review` is pure, so the rules of the builder are pinned
here without a database: the score and the pass are the code's, counted from
the rows (``TASK.md`` locks 1 and 2); ``passed`` never comes from the
explanation; the remarks go heaviest first, then by the criterion's number;
the review is in the explanation's language. Reading the rows and the
explanation from the database, and the refusal when either is missing, are
pinned through the body in ``tests/integration/test_text_review_e2e_db.py``.
"""

from __future__ import annotations

import pytest

from course_supporter.criteria_kinds import (
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_result import (
    REVIEW_PARTS_MISSING,
    build_text_review,
)
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
)
from course_supporter.models.review_structure import Remark, Verdict
from course_supporter.storage.submission_verdict_repository import VerdictRecord


def _criterion(
    cid: str, weight: WeightCategory, verdict: VerdictValue
) -> VerdictRecord:
    return VerdictRecord(cid, VerdictItemKind.CRITERION, weight, verdict, verdict)


def _point(pid: str, verdict: VerdictValue) -> VerdictRecord:
    return VerdictRecord(pid, VerdictItemKind.POINT, None, verdict, verdict)


def _remark(cid: str) -> CriterionRemark:
    return CriterionRemark(
        id=cid,
        what=f"Як зроблено {cid}.",
        why=f"Чому {cid} не гаразд.",
        todo=f"Як зробити {cid}.",
    )


def _explanation(
    *remarks: str, passed: bool = True, voice: str | None = "Власне слово Ментора."
) -> ExplanationAnswer:
    return ExplanationAnswer(
        passed=passed,
        why="Причина вердикту за курсом.",
        remarks=tuple(_remark(cid) for cid in remarks),
        mentor_voice=voice,
    )


MET, NOT_MET = VerdictValue.MET, VerdictValue.NOT_MET
MUST, SHOULD, MAY = WeightCategory.MUST, WeightCategory.SHOULD, WeightCategory.MAY


class TestTheScore:
    """Lock 1: the score is the hand count — weights 3 / 2 / 1, points inside."""

    def test_a_criterion_with_points_counts_by_its_own_row(self) -> None:
        """c1 must (met), c2 should with points (one not met), c3 may (met).

        By hand: (3 + 1) of (3 + 2 + 1) = 4 / 6 = 66 %, rounded down. The
        point that is met does not count on its own — its criterion does.
        """
        rows = [
            _criterion("c1", MUST, MET),
            _criterion("c2", SHOULD, NOT_MET),
            _point("c2.p1", MET),
            _point("c2.p2", NOT_MET),
            _criterion("c3", MAY, MET),
        ]

        _, score = build_text_review(
            rows=rows, explanation=_explanation("c2"), language="ukr"
        )

        assert score == 66

    def test_everything_met_is_a_hundred(self) -> None:
        rows = [_criterion("c1", MUST, MET), _criterion("c2", MAY, MET)]

        _, score = build_text_review(
            rows=rows, explanation=_explanation(), language="ukr"
        )

        assert score == 100


class TestThePassIsTheCodes:
    """Lock 2, and the builder's own rule: ``passed`` is counted, not read."""

    def test_an_unmet_must_fails_whatever_the_explanation_says(self) -> None:
        rows = [_criterion("c1", MUST, NOT_MET), _criterion("c2", SHOULD, MET)]

        structure, score = build_text_review(
            rows=rows, explanation=_explanation("c1", passed=True), language="ukr"
        )

        assert structure.verdict == Verdict(
            passed=False, why="Причина вердикту за курсом."
        )
        assert score == 40

    def test_every_must_met_passes_whatever_the_explanation_says(self) -> None:
        rows = [_criterion("c1", MUST, MET), _criterion("c2", SHOULD, NOT_MET)]

        structure, _ = build_text_review(
            rows=rows, explanation=_explanation("c2", passed=False), language="ukr"
        )

        assert structure.verdict is not None
        assert structure.verdict.passed is True

    def test_a_list_without_a_must_is_passed(self) -> None:
        rows = [_criterion("c1", SHOULD, NOT_MET), _criterion("c2", MAY, NOT_MET)]

        structure, score = build_text_review(
            rows=rows, explanation=_explanation("c1", "c2"), language="ukr"
        )

        assert structure.verdict is not None
        assert structure.verdict.passed is True
        assert score == 0


class TestTheRemarks:
    def test_the_heaviest_criterion_first_then_by_number(self) -> None:
        """``c10`` after ``c2``: by the number, not by the text of the id."""
        rows = [
            _criterion("c1", MAY, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MUST, NOT_MET),
            _criterion("c10", SHOULD, NOT_MET),
            _criterion("c11", MUST, NOT_MET),
        ]

        structure, _ = build_text_review(
            rows=rows,
            explanation=_explanation("c1", "c10", "c2", "c11", "c3"),
            language="ukr",
        )

        assert [remark.what for remark in structure.new_remarks] == [
            "Як зроблено c3.",
            "Як зроблено c11.",
            "Як зроблено c2.",
            "Як зроблено c10.",
            "Як зроблено c1.",
        ]

    def test_a_remark_keeps_the_three_parts_and_nothing_else(self) -> None:
        rows = [_criterion("c1", MUST, NOT_MET)]

        structure, _ = build_text_review(
            rows=rows, explanation=_explanation("c1"), language="ukr"
        )

        assert structure.new_remarks == [
            Remark(
                what="Як зроблено c1.", why="Чому c1 не гаразд.", todo="Як зробити c1."
            )
        ]

    def test_a_remark_on_an_item_without_a_row_is_a_defect(self) -> None:
        rows = [_criterion("c1", MUST, NOT_MET)]

        with pytest.raises(ValueError, match=r"\['c7'\]"):
            build_text_review(
                rows=rows, explanation=_explanation("c1", "c7"), language="ukr"
            )

    def test_a_remark_on_a_point_is_a_defect(self) -> None:
        """Remarks are on criteria; a point's id has no place among them."""
        rows = [_criterion("c1", MUST, NOT_MET), _point("c1.p1", NOT_MET)]

        with pytest.raises(ValueError, match=r"\['c1\.p1'\]"):
            build_text_review(
                rows=rows, explanation=_explanation("c1.p1"), language="ukr"
            )


class TestTheRestOfTheReview:
    def test_the_language_and_the_voice_are_the_explanations(self) -> None:
        rows = [_criterion("c1", MUST, MET)]

        structure, _ = build_text_review(
            rows=rows, explanation=_explanation(), language="eng"
        )

        assert structure.language == "eng"
        assert structure.mentor_voice == "Власне слово Ментора."
        assert structure.schema_version == "1"

    @pytest.mark.parametrize("voice", [None, "", "  \n"])
    def test_a_blank_voice_is_left_out(self, voice: str | None) -> None:
        rows = [_criterion("c1", MUST, MET)]

        structure, _ = build_text_review(
            rows=rows, explanation=_explanation(voice=voice), language="ukr"
        )

        assert structure.mentor_voice is None

    def test_the_markdown_is_the_assemblers(self) -> None:
        """Nothing in the builder writes text: the one assembler does."""
        rows = [_criterion("c1", MUST, NOT_MET)]
        structure, _ = build_text_review(
            rows=rows, explanation=_explanation("c1"), language="ukr"
        )

        markdown = assemble_review(structure)

        assert "Причина вердикту за курсом." in markdown
        assert "Як зроблено c1." in markdown
        assert "Власне слово Ментора." in markdown

    def test_rows_without_a_criterion_have_no_review(self) -> None:
        with pytest.raises(ValueError, match="without a criterion"):
            build_text_review(
                rows=[_point("c1.p1", MET)], explanation=_explanation(), language="ukr"
            )

    def test_the_refusal_code_is_a_short_key(self) -> None:
        assert REVIEW_PARTS_MISSING == "review_parts_missing"
