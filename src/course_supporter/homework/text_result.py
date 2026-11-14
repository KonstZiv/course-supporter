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
    * The pass rule depends on whose list the rows were judged against — the
      layer of the address the rows carry, read by
      :func:`~course_supporter.homework.judged_list.judged_layer`, the same
      address the course links read the list by. ``verdict.rule`` keeps the
      branch that decided, the score and the threshold; the assembler prints
      its phrase under the verdict (2026-11-12).
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

The place of a remark (project chain K5):
    A remark points to where its problem stands in the student's files when
    its verdict row keeps one — a project's "not met" whose line was found
    (``homework/criteria_verdicts.py``). A criterion checked by mandatory
    points points to the place of its first point, in the list's order, that
    is not met and has one. ``Remark.position`` is ``file``; its value is the
    path, then ``:12`` for one line or ``:12-14`` for several — the phrase of
    the place stays the phrasebook's, ``position.file``. A text type's rows
    keep no place, so its review is what it was. :func:`problem_positions`
    is the rule; a project's builder takes it as it is.

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
from typing import Final, Protocol

from course_supporter.criteria_kinds import (
    CriteriaLayer,
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
    pass_decision,
)
from course_supporter.homework.judged_list import judged_layer
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
    PassRule,
    Position,
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
    "ReviewedItem",
    "TextResultBuilder",
    "build_text_review",
    "link_authenticity",
    "problem_positions",
]

REVIEW_PARTS_MISSING: Final = "review_parts_missing"
"""The code a text task's submission fails with when its review has no parts.

The verdict rows or the explanation the review is built from are not there.
A failure on our side, not the student's: nothing in the work caused it.
"""


class ReviewedItem(ScoredItem, Protocol):
    """What the review's builder reads of a verdict row: the score's fields,
    and the place its quote was found.

    A stored row (``storage.orm.SubmissionCriterionVerdict``) and a
    :class:`~course_supporter.storage.submission_verdict_repository.VerdictRecord`
    both have these.
    """

    @property
    def quote_file(self) -> str | None: ...

    @property
    def quote_line_start(self) -> int | None: ...

    @property
    def quote_line_end(self) -> int | None: ...


def problem_positions(rows: Iterable[ReviewedItem]) -> dict[str, Position]:
    """By criterion id, where in the student's files its remark points (K5).

    Read from the rows that are "not met" and keep a place: the row of a
    criterion judged on its own, or the first point of a criterion checked
    by points, in the rows' order — the list's, in which the evaluation
    writes them and the repository reads them back. A "met"'s place is its
    evidence, never a problem's, and is not read.

    >>> from course_supporter.criteria_kinds import VerdictItemKind as Kind
    >>> from course_supporter.storage.submission_verdict_repository import (
    ...     VerdictRecord,
    ... )
    >>> rows = [
    ...     VerdictRecord("c2", Kind.CRITERION, "should", "not_met", None),
    ...     VerdictRecord("c2.p1", Kind.POINT, None, "not_met", "not_met"),
    ...     VerdictRecord("c2.p2", Kind.POINT, None, "not_met", "not_met",
    ...                   quote="x", quote_file="app.py", quote_lines=(12, 14)),
    ... ]
    >>> problem_positions(rows)["c2"].value
    'app.py:12-14'
    """
    positions: dict[str, Position] = {}
    for row in rows:
        if row.verdict != VerdictValue.NOT_MET or row.quote_file is None:
            continue
        # ``c3`` for the criterion itself, ``c3.p2`` for one of its points.
        criterion_id = row.item_id.partition(".")[0]
        if criterion_id not in positions:
            positions[criterion_id] = Position(kind="file", value=_place_of(row))
    return positions


def _place_of(row: ReviewedItem) -> str:
    """``path``, ``path:12`` or ``path:12-14`` — the value of a file's position.

    An ASCII hyphen and no words: the value goes into the phrase of the
    review's language unchanged.
    """
    path = row.quote_file or ""
    first, last = row.quote_line_start, row.quote_line_end
    if first is None or last is None:
        return path
    if first == last:
        return f"{path}:{first}"
    return f"{path}:{first}-{last}"


def build_text_review(
    *,
    rows: Sequence[ReviewedItem],
    layer: CriteriaLayer,
    explanation: ExplanationAnswer,
    language: str,
    reading: Mapping[str, Sequence[Reference]] | None = None,
) -> tuple[ReviewStructureV1, int]:
    """A text task's review as a structure, and its score — a pure function.

    Args:
        rows: The submission's verdict rows, criteria and points alike.
        layer: Whose list the rows were judged against: the pass rule
            depends on it.
        explanation: The explanation stage's answer.
        language: The language the explanation is written in.
        reading: By criterion id, what its remark gives to read — the course
            links settled for it; a criterion missing here gives none.

    Raises:
        ValueError: The rows have no criterion, or a criterion without a
            weight (:func:`~course_supporter.homework.criteria_verdicts.score_percent`);
            or the explanation remarks on a criterion the rows do not have.
    """
    decided = pass_decision(rows, layer=layer)
    places = problem_positions(rows)
    voice = explanation.mentor_voice
    structure = ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=language,
        verdict=Verdict(
            passed=decided.passed,
            why=explanation.why,
            rule=PassRule(
                basis=decided.basis,
                score=decided.score,
                threshold=decided.threshold,
            ),
        ),
        new_remarks=[
            Remark(
                what=remark.what,
                why=remark.why,
                todo=remark.todo,
                read=list((reading or {}).get(remark.id, ())),
                position=places.get(remark.id),
            )
            for remark in _by_weight(explanation.remarks, rows)
        ],
        # A blank voice is no voice: the assembler would print its block empty.
        mentor_voice=voice if voice is not None and voice.strip() else None,
    )
    return structure, decided.score


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
            layer=judged_layer(rows),
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
