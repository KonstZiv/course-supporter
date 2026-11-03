"""A text task's review, built from its verdicts and their explanation (task 09b).

Purpose:
    The result builder of ``task`` and ``short_task``
    (``homework/result_builders.py``). The two stages before it have written
    everything the review says: the evaluation stage a verdict on every
    criterion and point (``homework/criteria_evaluation.py``), the
    explanation stage the words around them (``homework/review_explanation.py``)
    and the places of the course it chose to link (task 10). The builder
    reads, counts and lays out — no model call. It writes one thing: the
    course links it gives, checked before they are given and kept so that a
    second build of the same review gives the same
    (``homework/course_link_delivery.py``); they are committed with the
    review they belong to.

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
    * The course links are the model's choice among the places the code
      showed it, resolved and checked by code
      (:func:`~course_supporter.homework.course_link_delivery.settle_course_links`):
      a remark reads the places of its criterion, never another's.

After delivery (task 10, K6):
    The review's authenticity goes to the call register
    (:func:`~course_supporter.service_logging.record_review_metrics`): the
    share of the criteria not met whose remark gives at least one course
    link. A review with every criterion met makes no claim to support and
    leaves no row. Completeness is not measured on this path (``None``):
    the explanation's check already refuses an answer without a remark on
    every criterion not met. The row is written once per submission: a run
    re-entered after delivery delivers again, and finds the row there.

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

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from course_supporter.criteria_kinds import (
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.course_link_delivery import (
    reference_of,
    settle_course_links,
)
from course_supporter.homework.course_links import FoundCourseLinks
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
    Reference,
    Remark,
    ReviewStructureV1,
    Verdict,
)
from course_supporter.review_metrics import (
    Claim,
    ReviewMetrics,
    ShareMetricsCalculator,
)
from course_supporter.service_logging import record_review_metrics
from course_supporter.storage.repositories import HomeworkCostRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

__all__ = [
    "REVIEW_PARTS_MISSING",
    "TextResultBuilder",
    "build_text_review",
    "link_authenticity",
]

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
    reading: Mapping[str, Sequence[Reference]] | None = None,
) -> tuple[ReviewStructureV1, int]:
    """A text task's review as a structure, and its score — a pure function.

    Args:
        rows: The submission's verdict rows, criteria and points alike.
        explanation: The explanation stage's answer.
        language: The language the explanation is written in.
        reading: By criterion id, what its remark gives to read — the course
            links settled for it; a criterion missing here gives none.

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
            Remark(
                what=remark.what,
                why=remark.why,
                todo=remark.todo,
                read=list((reading or {}).get(remark.id, ())),
            )
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


def link_authenticity(
    unmet: Sequence[str], links: Mapping[str, Sequence[object]]
) -> float | None:
    """The share of ``unmet`` criteria whose remark gives a course link.

    ``None`` when no criterion is unmet: such a review makes no claim a link
    could support, and zero would say it made one unsupported.

    >>> link_authenticity(["c1", "c3"], {"c1": ["a link"], "c3": []})
    0.5
    >>> link_authenticity([], {}) is None
    True
    """
    claims = [
        # Every unmet criterion has its remark (the explanation's check).
        Claim(supported_by_reference=bool(links.get(cid)), covered_by_verdict=True)
        for cid in unmet
    ]
    return ShareMetricsCalculator().calculate(claims).authenticity


class TextResultBuilder:
    """The result of a text task: counted by code, worded by the explanation."""

    async def build(self, context: BuildContext) -> BuiltResult:
        """Read the verdicts and their explanation, and lay out the review.

        Through the submission's own session, with the tenant on each read
        (``impl-rules#9``). The one write — the course links given, settled
        (:func:`~course_supporter.homework.course_link_delivery.settle_course_links`)
        — is flushed into the same session, for the body to commit with the
        review.

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
        answer = ExplanationAnswer.model_validate(explained.body)
        links = await settle_course_links(
            context.session, context.session_factory, submission, answer, rows
        )
        structure, score = build_text_review(
            rows=rows,
            explanation=answer,
            language=explained.language,
            reading={
                criterion_id: [reference_of(place) for place in places]
                for criterion_id, places in links.items()
            },
        )
        return BuiltResult(
            structure=structure, markdown=assemble_review(structure), score=score
        )

    async def after_delivery(self, context: BuildContext) -> None:
        """Write the review's authenticity to the call register (module).

        Read from what was kept — the verdict rows and the links settled for
        the review. Once per submission: a run re-entered after delivery
        (from ``completed``, which builds nothing) finds the row of the
        first and writes no second.
        """
        submission = context.submission
        if await HomeworkCostRepository(context.session).has_review_metrics(
            tenant_id=submission.tenant_id, submission_id=submission.id
        ):
            return
        stored = SubmissionVerdictRepository(context.session)
        rows = await stored.list_for_submission(
            tenant_id=submission.tenant_id, submission_id=submission.id
        )
        unmet = [
            row.item_id
            for row in rows
            if row.item_kind == VerdictItemKind.CRITERION
            and row.verdict == VerdictValue.NOT_MET
        ]
        if not unmet:
            return
        kept = await stored.get_candidates(
            tenant_id=submission.tenant_id, submission_id=submission.id
        )
        found = (
            FoundCourseLinks.model_validate(kept.body)
            if kept is not None
            else FoundCourseLinks(criteria=())
        )
        await record_review_metrics(
            context.session_factory,
            ReviewMetrics(
                authenticity=link_authenticity(
                    unmet, {cid: found.settled_of(cid) for cid in unmet}
                ),
                completeness=None,
            ),
        )
