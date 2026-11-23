"""A text task's review, built from its verdicts and their explanation (task 09b).

Purpose:
    The result builder of ``task`` and ``short_task``
    (``homework/result_builders.py``), and of ``project`` as it is (project
    chain K6): a project's stages are the same two functions under their own
    names, and write the same rows and the same explanation. The two stages
    before it have written
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
      address the course links read the list by — and on the "must"
      criteria the author made mandatory themselves, read from that list
      (:func:`~course_supporter.homework.judged_list.judged_author_musts`,
      2026-11-17). ``verdict.rule`` keeps the
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
    is the rule; a project's builder -- this one, since chain K6 -- takes it
    as it is.

The order of the remarks:
    By their criterion's weight — "must" first, then "should", then "may" —
    and within one weight by the criterion's number, ``c2`` before ``c10``:
    what decides the pass is read first. The same order in every section.

The sections of the remarks (task 12a, K2):
    Each remark goes to the section of its criterion's state since the
    student's earlier attempt at the same version of the task
    (:func:`~course_supporter.homework.criteria_verdicts.remark_states`):
    "new", or no state — ``new_remarks``; "open" — ``open``; "broken" —
    ``broken``. A criterion "fixed" is in ``fixed``, worded by its own remark
    in the earlier attempt's explanation — without that remark's reading
    and place, which were the earlier work's — or, when that explanation has
    none, by the criterion's own words. Its remark of this submission, if
    any — one is given only to a criterion unconfirmed now — stays new.
    Points have states too, but a remark is a criterion's: a criterion's
    own state places it. With nothing to compare with — a first attempt —
    every remark is new, as before the states. Every remark, in each of the
    four sections, names its criterion (``Remark.item_id``): the portal ties
    a student's reply to the remark by it.

    The states are the verdict rows' own (``remark_state``): the evaluation
    stage counts and writes them with the rows
    (:func:`~course_supporter.homework.criteria_verdicts.with_remark_states`),
    and the builder reads them — a row written before the states has none,
    and its remark is new. The earlier attempt
    (:meth:`~course_supporter.storage.submission_verdict_repository.SubmissionVerdictRepository.earlier_attempt`)
    is read here only for the words of a fixed criterion.

The replies (task 12a, K4):
    The section ``replies`` holds each reply of the student this review
    answered and the answer, in the order of the replies, as K1's table
    holds them (:func:`replies_answered`); a hostile reply's words are not
    repeated in it (``reply.said.withheld``). Only this section is added: the
    rest of the review is built as before.

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
    review itself is :func:`build_text_review`, a pure function of the rows,
    the explanation and the states of the remarks.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework import remark_comments
from course_supporter.homework.course_link_delivery import (
    reference_of,
    settle_course_links,
)
from course_supporter.homework.course_links import FoundCourseLinks
from course_supporter.homework.criteria_form import WEIGHT_NUMBERS, Criterion
from course_supporter.homework.criteria_verdicts import (
    ScoredItem,
    pass_decision,
)
from course_supporter.homework.earlier_review import read_earlier_review
from course_supporter.homework.judged_list import (
    judged_author_musts,
    judged_layer,
    read_judged_list,
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
    PassRule,
    Position,
    Reference,
    Remark,
    Reply,
    ReplyKind,
    ReviewStructureV1,
    Verdict,
)
from course_supporter.phrasebook import phrases_for
from course_supporter.remark_kinds import CommentKind, CommentTopic, RemarkState
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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )

__all__ = [
    "REVIEW_PARTS_MISSING",
    "FixedWords",
    "ReviewedItem",
    "TextResultBuilder",
    "build_text_review",
    "link_authenticity",
    "problem_positions",
    "replies_answered",
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


@dataclass(frozen=True, slots=True)
class FixedWords:
    """The words of the criteria fixed since the earlier attempt (task 12a).

    Attributes:
        remarks: The earlier attempt's remarks, by criterion id: a fixed
            criterion's own, which the student has dealt with.
        criteria: The criteria the rows were judged against, by id: the
            words of a fixed criterion the earlier explanation has no remark
            on.
    """

    remarks: Mapping[str, CriterionRemark]
    criteria: Mapping[str, Criterion]


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
    author_musts: Collection[str],
    explanation: ExplanationAnswer,
    language: str,
    reading: Mapping[str, Sequence[Reference]] | None = None,
    states: Mapping[str, RemarkState | None] | None = None,
    fixed_words: FixedWords | None = None,
    replies: Sequence[Reply] = (),
) -> tuple[ReviewStructureV1, int]:
    """A text task's review as a structure, and its score — a pure function.

    Args:
        rows: The submission's verdict rows, criteria and points alike.
        layer: Whose list the rows were judged against: the pass rule
            depends on it.
        author_musts: The criteria of that list the author made mandatory
            themselves: the pass rule depends on them too.
        explanation: The explanation stage's answer.
        language: The language the explanation is written in.
        reading: By criterion id, what its remark gives to read — the course
            links settled for it; a criterion missing here gives none.
        states: By item id, where its remark stands since the student's
            earlier attempt (task 12a): the section it goes to (module). An
            item missing here has no state; ``None`` — none has, and every
            remark is new.
        fixed_words: The words of the criteria ``states`` names fixed.
        replies: What the student wrote to the earlier review and what this
            review answered, in the order of the replies (task 12a, K4).

    Raises:
        ValueError: The rows have no criterion, or a criterion without a
            weight (:func:`~course_supporter.homework.criteria_verdicts.score_percent`);
            or the explanation remarks on a criterion the rows do not have;
            or a criterion is fixed and ``fixed_words`` has no words for it.
    """
    decided = pass_decision(rows, layer=layer, author_musts=author_musts)
    places = problem_positions(rows)
    order = _weight_order(rows)
    state_of = states or {}
    new_remarks: list[Remark] = []
    still_open: list[Remark] = []
    broken: list[Remark] = []
    section_of: dict[RemarkState | None, list[Remark]] = {
        None: new_remarks,
        RemarkState.NEW: new_remarks,
        RemarkState.OPEN: still_open,
        RemarkState.BROKEN: broken,
        # A criterion fixed since the earlier attempt has a remark now only
        # when it is unconfirmed: that is new. Its earlier remark is in fixed.
        RemarkState.FIXED: new_remarks,
    }
    for remark in _by_weight(explanation.remarks, order):
        section_of[state_of.get(remark.id)].append(
            Remark(
                what=remark.what,
                why=remark.why,
                todo=remark.todo,
                read=list((reading or {}).get(remark.id, ())),
                position=places.get(remark.id),
                item_id=remark.id,
            )
        )
    fixed = [
        _fixed_remark(criterion_id, fixed_words)
        for criterion_id in sorted(order, key=order.__getitem__)
        if state_of.get(criterion_id) is RemarkState.FIXED
    ]
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
        fixed=fixed,
        new_remarks=new_remarks,
        open=still_open,
        broken=broken,
        # A blank voice is no voice: the assembler would print its block empty.
        mentor_voice=voice if voice is not None and voice.strip() else None,
        replies=list(replies),
    )
    return structure, decided.score


async def replies_answered(
    session: AsyncSession, submission: HomeworkSubmission, *, language: str
) -> list[Reply]:
    """The review's section of replies: each reply it answered, with the answer.

    Task 12a, K4. Read from K1's table — the answers this submission's
    review gave, each with its reply, in the order of the replies
    (``answers_in_review`` of K3's store) — so the review says what the
    table holds. ``said`` is the student's text from the table; for a hostile
    reply it is the phrasebook's ``reply.said.withheld`` in the review's
    ``language``, never the reply's text: a review is read by the school's
    own systems too. No answer — no section.
    """
    answered = await remark_comments.build_remark_comment_repository(
        session
    ).answers_in_review(tenant_id=submission.tenant_id, submission_id=submission.id)
    if not answered:
        return []
    withheld = phrases_for(language)["reply.said.withheld"]
    return [
        Reply(
            kind=_reply_kind(pair.reply.kind),
            said=(
                withheld
                if pair.reply.topic is CommentTopic.HOSTILE
                else pair.reply.body
            ),
            answer=pair.answer.body,
        )
        for pair in answered
    ]


def _reply_kind(kind: CommentKind) -> ReplyKind:
    """A student's kind of comment as the review names it — never ``answer``."""
    if kind is CommentKind.QUESTION:
        return "question"
    if kind is CommentKind.OBJECTION:
        return "objection"
    if kind is CommentKind.COMMENT:
        return "comment"
    msg = f"a reply is a student's comment, not an {kind.value}"
    raise ValueError(msg)


def _weight_order(rows: Iterable[ScoredItem]) -> dict[str, tuple[int, int]]:
    """By criterion id, its place among the remarks: heaviest first, then by number."""
    return {
        row.item_id: (
            -WEIGHT_NUMBERS[WeightCategory(row.weight)],
            int(row.item_id.removeprefix("c")),
        )
        for row in rows
        if row.item_kind == VerdictItemKind.CRITERION and row.weight is not None
    }


def _by_weight(
    remarks: Iterable[CriterionRemark], order: Mapping[str, tuple[int, int]]
) -> list[CriterionRemark]:
    """The remarks, heaviest criterion first, then by the criterion's number."""
    ordered = list(remarks)
    strangers = sorted({remark.id for remark in ordered} - order.keys())
    if strangers:
        msg = f"the explanation remarks on {strangers}, which have no verdict rows"
        raise ValueError(msg)
    return sorted(ordered, key=lambda remark: order[remark.id])


def _fixed_remark(criterion_id: str, words: FixedWords | None) -> Remark:
    """How the ``fixed`` section shows a criterion fixed since the earlier attempt.

    Its own remark in the earlier attempt's explanation — what the student
    has dealt with — without its reading and its place, which were the
    earlier work's. When that explanation has none (its check gives one to
    every criterion not met, so this is a defect of the data, not a reason to
    fail the review), the criterion's own words, in the course's language:
    what it requires, and what in a work shows it — no phrase of the
    phrasebook says more.
    """
    earlier = words.remarks.get(criterion_id) if words is not None else None
    if earlier is not None:
        return Remark(
            what=earlier.what,
            why=earlier.why,
            todo=earlier.todo,
            item_id=criterion_id,
        )
    criterion = words.criteria.get(criterion_id) if words is not None else None
    if criterion is None:
        msg = f"criterion {criterion_id} is fixed, and there are no words for it"
        raise ValueError(msg)
    return Remark(
        what=criterion.text,
        why=criterion.text,
        todo=criterion.evidence,
        item_id=criterion_id,
    )


async def _fixed_words(
    session: AsyncSession,
    submission: HomeworkSubmission,
    rows: Sequence[SubmissionCriterionVerdict],
    *,
    states: Mapping[str, RemarkState | None],
) -> FixedWords | None:
    """The words of the fixed criteria, read only when a criterion is fixed.

    The explanation of the earlier attempt the stage compared with
    (:func:`~course_supporter.homework.earlier_review.read_earlier_review`,
    the same read as the evaluation stage's), when
    there is one, and the criteria the rows were judged against
    (:func:`~course_supporter.homework.judged_list.read_judged_list`) for a
    fixed criterion that explanation has no remark on — or when an attempt
    reviewed since the stage ran is the earlier one now and has none.
    """
    if RemarkState.FIXED not in states.values():
        return None
    earlier = await read_earlier_review(session, submission)
    criteria = await read_judged_list(session, submission, rows)
    return FixedWords(
        remarks=dict(earlier.remarks) if earlier is not None else {},
        criteria={criterion.id: criterion for criterion in criteria},
    )


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
        # The states the evaluation stage wrote with the rows (task 12a).
        states = {
            row.item_id: RemarkState(row.remark_state) if row.remark_state else None
            for row in rows
        }
        structure, score = build_text_review(
            rows=rows,
            layer=judged_layer(rows),
            author_musts=await judged_author_musts(context.session, submission, rows),
            explanation=answer,
            language=explained.language,
            reading={
                criterion_id: [reference_of(place) for place in places]
                for criterion_id, places in links.items()
            },
            states=states,
            fixed_words=await _fixed_words(
                context.session, submission, rows, states=states
            ),
            replies=await replies_answered(
                context.session, submission, language=explained.language
            ),
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
