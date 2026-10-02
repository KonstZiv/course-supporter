"""A text task's review, built from its verdicts and their explanation (task 09b).

Purpose:
    The result builder of ``task`` and ``short_task``
    (``homework/result_builders.py``). The two stages before it have written
    everything the review says: the evaluation stage a verdict on every
    criterion and point (``homework/criteria_evaluation.py``), the
    explanation stage the words around them (``homework/review_explanation.py``).
    The builder reads, counts and lays out — no model call, nothing written.

What is the code's and what is the model's (``PRE-PLAN.md`` decisions 2, 3, 5):
    * The score and the pass are counted here from the stored rows, by the
      evaluation's own functions (``homework/criteria_verdicts.py``): the
      count the stages made, with nothing derived stored beside the rows to
      drift from it.
    * ``verdict.passed`` is that count — never the explanation's repetition
      of it. The explanation is checked against the facts when it is written
      (``homework/verdict_explanation.py``); the field a school's platform
      gates on does not rest on that check having held.
    * The reason, the remarks and the Mentor's own word are the explanation's.

The order of the remarks:
    By their criterion's weight — "must" first, then "should", then "may" —
    and within one weight by the criterion's number, ``c2`` before ``c10``:
    what decides the pass is read first.

Language:
    The explanation's own: the language its texts are written in, as the
    explanation stage resolved it (``PRE-FLIGHT.md`` 9.9) — not the doors'
    resolution handed to the builder, which may be none.

Failure:
    A submission without verdict rows or without an explanation was not
    reviewed by the stages this builder reads — its path did not list them,
    or what they wrote is gone — and fails with :data:`REVIEW_PARTS_MISSING`.

Replacing:
    The body knows the builder by its two methods
    (:class:`~course_supporter.homework.result_builders.ResultBuilder`); the
    review itself is :func:`build_text_review`, a pure function of the rows
    and the explanation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from course_supporter.criteria_kinds import VerdictItemKind, WeightCategory
from course_supporter.homework.criteria_form import WEIGHT_NUMBERS
from course_supporter.homework.criteria_verdicts import (
    ScoredItem,
    is_passed,
    score_percent,
)
from course_supporter.homework.result_builders import (
    BuildContext,
    BuiltResult,
    ResultNotBuiltError,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
)
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    Remark,
    ReviewStructureV1,
    Verdict,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

__all__ = ["REVIEW_PARTS_MISSING", "TextResultBuilder", "build_text_review"]

REVIEW_PARTS_MISSING: Final = "review_parts_missing"
"""The code a text task's submission fails with when its review has no parts.

The verdict rows or the explanation the review is built from are not there.
A failure on our side, not the student's: nothing in the work caused it.
"""


def build_text_review(
    *,
    rows: Sequence[ScoredItem],
    explanation: ExplanationAnswer,
    language: str,
) -> tuple[ReviewStructureV1, int]:
    """A text task's review as a structure, and its score — a pure function.

    Args:
        rows: The submission's verdict rows, criteria and points alike.
        explanation: The explanation stage's answer.
        language: The language the explanation is written in.

    Raises:
        ValueError: The rows have no criterion, or a criterion without a
            weight (:func:`~course_supporter.homework.criteria_verdicts.score_percent`);
            or the explanation remarks on a criterion the rows do not have.
    """
    score = score_percent(rows)
    voice = explanation.mentor_voice
    structure = ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=language,
        verdict=Verdict(passed=is_passed(rows), why=explanation.why),
        new_remarks=[
            Remark(what=remark.what, why=remark.why, todo=remark.todo)
            for remark in _by_weight(explanation.remarks, rows)
        ],
        # A blank voice is no voice: the assembler would print its block empty.
        mentor_voice=voice if voice is not None and voice.strip() else None,
    )
    return structure, score


def _by_weight(
    remarks: Iterable[CriterionRemark], rows: Iterable[ScoredItem]
) -> list[CriterionRemark]:
    """The remarks, heaviest criterion first, then by the criterion's number."""
    place: dict[str, tuple[int, int]] = {
        row.item_id: (
            -WEIGHT_NUMBERS[WeightCategory(row.weight)],
            int(row.item_id.removeprefix("c")),
        )
        for row in rows
        if row.item_kind == VerdictItemKind.CRITERION and row.weight is not None
    }
    ordered = list(remarks)
    strangers = sorted({remark.id for remark in ordered} - place.keys())
    if strangers:
        msg = f"the explanation remarks on {strangers}, which have no verdict rows"
        raise ValueError(msg)
    return sorted(ordered, key=lambda remark: place[remark.id])


class TextResultBuilder:
    """The result of a text task: counted by code, worded by the explanation."""

    async def build(self, context: BuildContext) -> BuiltResult:
        """Read the verdicts and their explanation, and lay out the review.

        Through the submission's own session, reading only, with the tenant
        on each read (``impl-rules#9``).

        Raises:
            ResultNotBuiltError: :data:`REVIEW_PARTS_MISSING` — no verdict
                rows, or no explanation.
            ValueError: See :func:`build_text_review`; and an explanation
                that is not of the stage's form — our own data, failed with
                the body's generic code.
        """
        submission = context.submission
        stored = SubmissionVerdictRepository(context.session)
        rows = await stored.list_for_submission(
            tenant_id=submission.tenant_id, submission_id=submission.id
        )
        if not rows:
            raise ResultNotBuiltError(
                REVIEW_PARTS_MISSING, "the submission has no verdict rows"
            )
        explained = await stored.get_explanation(
            tenant_id=submission.tenant_id, submission_id=submission.id
        )
        if explained is None:
            raise ResultNotBuiltError(
                REVIEW_PARTS_MISSING, "the submission has no explanation"
            )
        structure, score = build_text_review(
            rows=rows,
            explanation=ExplanationAnswer.model_validate(explained.body),
            language=explained.language,
        )
        return BuiltResult(
            structure=structure, markdown=assemble_review(structure), score=score
        )

    async def after_delivery(self, context: BuildContext) -> None:
        """Nothing follows a text task's review: all it says is written before."""
