"""Reads and writes of a submission's verdicts on criteria (mentor-rebuild task 09b).

Purpose:
    The evaluation stage writes a submission's verdicts; the explanation stage
    and the review's builder read them; the next submission of the same student
    reads them back through the resubmission safeguard, and its review compares
    its remarks with them (task 12a). Each of these has to remember the same
    filters — the tenant, and the live submission — and a
    rule spread over several call sites is a rule that drifts, so all of them
    live here, with the explanation stage's answer beside them — and, since
    task 10, the course links the search found for the failed criteria.

Interface:
    :class:`VerdictRecord` — one verdict as a stage hands it in.
    :class:`EarlierAttempt` — the attempt a submission's remarks are compared
    with (task 12a), as ``earlier_attempt`` reads it.
    :class:`SubmissionVerdictRepository` — eight methods:
    ``replace_for_submission``, ``list_for_submission``, ``previous_verdicts``,
    ``earlier_attempt``, ``store_explanation``, ``get_explanation``,
    ``store_candidates`` and ``get_candidates``.

    Writes ``flush`` and never commit: the caller owns the transaction, so a
    stage's verdicts are committed together with the checkpoint that marks the
    stage done (``homework/path_runner.py``).

Replacing:
    The stages depend on these eight methods and on the value types only;
    another store implements the same eight without touching a stage.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    RepeatReason,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.remark_kinds import RemarkState
from course_supporter.storage.homework_repository import ATTEMPT_STATUSES
from course_supporter.storage.orm import (
    HomeworkSubmission,
    SubmissionCourseLinkCandidates,
    SubmissionCriterionVerdict,
    SubmissionExplanation,
)


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """One verdict on one criterion or mandatory point, as a stage hands it in.

    Attributes:
        item_id: ``c3`` for a criterion, ``c3.p2`` for a point.
        item_kind: Criterion or point.
        weight: The criterion's category; ``None`` for a point.
        verdict: What stands.
        model_verdict: What the model said; ``None`` for a criterion derived
            from its points.
        quote: The evidence a "met" stands on, or the model's quote that was
            not found; on a project's "not met", the line where its problem
            stands, when the student's files have it (chain K5).
        quote_file: Where the quote was found; ``None`` when it was not.
        quote_lines: First and last line of the quote in ``quote_file``;
            ``None`` for a document, whose lines are an artefact of extraction.
        missing: The model's sentence on what is missing.
        retried: The item was asked about once more.
        repeat_reason: Why the item was asked about once more — the reason
            the repeated request named, decided on the model's first answer
            (``homework/criteria_verdicts.py``, ``_repeat_reason``), whatever
            the repeat then settled. ``None`` — the item was not asked about
            again; always so for a criterion derived from its points. Kept
            for the database alone (the cost of repeats by reason): no review,
            webhook or API reads it.
        quote_not_found: The model said "met" and no quote bore it out after
            the repeat — the repeat's quote did not stand either, or the
            repeat took back a "met" whose quote was too short but in the
            work: unconfirmed, the author's flag. On a criterion checked by
            mandatory points: every point is met or so, one at least so
            (``homework/criteria_verdicts.py``).
        quote_from_base: The item falls for a quote of the project's base,
            the author's code: the model's last "met" stood on one, or its
            first "met" did and the repeat showed none of the student's code
            — "not met", the author's flag, never unconfirmed (project chain
            K3). On a row the safeguard kept, as ``quote_not_found`` there:
            what the model proposed. On a criterion derived from its points:
            one of its points carries it.
        safeguard_submission_id: The earlier submission whose "met" the
            safeguard kept; ``None`` when it did not fire.
        first_model_verdict: What the model said in its first answer, on a
            retried item alone (``model_verdict`` is its word after the
            repeat); ``None`` for every other item. Kept for the database
            alone (whether a repeat changed the verdict): no review, webhook
            or API reads it.
        answered_by_call_id: The register row of the call whose answer the
            verdict stands on — of a retried item, the one
            ``criteria_verdicts._word_to_settle`` chose; ``None`` for a
            criterion derived from its points, or when the call's row was not
            written. For the database alone, as ``first_model_verdict``.
        remark_state: Where the item's remark stands since the student's
            earlier attempt at the same version of the task (task 12a,
            ``homework/criteria_verdicts.py``, ``with_remark_states``);
            ``None`` — no state. The review's builder lays its remarks out by
            it.
    """

    item_id: str
    item_kind: VerdictItemKind
    weight: WeightCategory | None
    verdict: VerdictValue
    model_verdict: VerdictValue | None
    quote: str | None = None
    quote_file: str | None = None
    quote_lines: tuple[int, int] | None = None
    missing: str | None = None
    retried: bool = False
    repeat_reason: RepeatReason | None = None
    quote_not_found: bool = False
    quote_from_base: bool = False
    safeguard_submission_id: uuid.UUID | None = None
    first_model_verdict: VerdictValue | None = None
    answered_by_call_id: uuid.UUID | None = None
    remark_state: RemarkState | None = None

    @property
    def quote_line_start(self) -> int | None:
        """The first line of :attr:`quote_lines`, as the stored row names it."""
        return self.quote_lines[0] if self.quote_lines else None

    @property
    def quote_line_end(self) -> int | None:
        """The last line of :attr:`quote_lines`, as the stored row names it."""
        return self.quote_lines[1] if self.quote_lines else None


@dataclass(frozen=True, slots=True)
class EarlierAttempt:
    """The student's earlier attempt a submission's remarks are compared with.

    Task 12a: each remark's state is counted from the two attempts' rows
    (``homework/criteria_verdicts.py``, ``remark_states``), and the review
    words a fixed one by this attempt's explanation.

    Attributes:
        submission_id: The earlier attempt's submission.
        rows: Its verdict rows, in the order they were written.
    """

    submission_id: uuid.UUID
    rows: tuple[SubmissionCriterionVerdict, ...]


class SubmissionVerdictRepository:
    """Storage for a submission's verdicts, its explanation and its course links."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_submission(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        criteria_layer: CriteriaLayer,
        criteria_source_id: uuid.UUID,
        records: Sequence[VerdictRecord],
    ) -> None:
        """Make ``records`` the submission's verdicts, whatever was there before.

        Deletes and inserts in one flush: a stage that runs again — after a
        crash between its write and its checkpoint — leaves one set of rows,
        not two. All records are judged against one list, so the list's
        address is given once.
        """
        await self._session.execute(
            delete(SubmissionCriterionVerdict).where(
                SubmissionCriterionVerdict.tenant_id == tenant_id,
                SubmissionCriterionVerdict.submission_id == submission_id,
            )
        )
        self._session.add_all(
            [
                _row(
                    record,
                    tenant_id=tenant_id,
                    submission_id=submission_id,
                    criteria_layer=criteria_layer,
                    criteria_source_id=criteria_source_id,
                )
                for record in records
            ]
        )
        await self._session.flush()

    async def list_for_submission(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> list[SubmissionCriterionVerdict]:
        """The submission's verdicts, in the order they were written."""
        stmt = (
            select(SubmissionCriterionVerdict)
            .where(
                SubmissionCriterionVerdict.tenant_id == tenant_id,
                SubmissionCriterionVerdict.submission_id == submission_id,
            )
            .order_by(SubmissionCriterionVerdict.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def previous_verdicts(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        criteria_layer: CriteriaLayer,
        criteria_source_id: uuid.UUID,
    ) -> dict[str, SubmissionCriterionVerdict]:
        """The last earlier verdict on each item of the same list, by item id.

        What the resubmission safeguard compares a new verdict with
        (``PRE-FLIGHT.md`` 9.8, ratified 2026-10-01): for every item judged
        against this very list, the verdict of the LATEST earlier submission of
        the same student on the same task that has one. A list is matched by
        its address, never by content — identifiers are stable within one list
        only (task 08, decision 21). A soft-deleted submission does not count;
        "earlier" is by creation time, then by id, so submissions that share a
        timestamp still have an order.
        """
        current = aliased(HomeworkSubmission)
        earlier = aliased(HomeworkSubmission)
        stmt = (
            select(SubmissionCriterionVerdict)
            .join(earlier, earlier.id == SubmissionCriterionVerdict.submission_id)
            .join(current, current.id == submission_id)
            .where(
                SubmissionCriterionVerdict.tenant_id == tenant_id,
                SubmissionCriterionVerdict.criteria_layer == criteria_layer.value,
                SubmissionCriterionVerdict.criteria_source_id == criteria_source_id,
                current.tenant_id == tenant_id,
                earlier.tenant_id == tenant_id,
                earlier.student_id == current.student_id,
                earlier.authored_document_id == current.authored_document_id,
                earlier.deleted_at.is_(None),
                tuple_(earlier.created_at, earlier.id)
                < tuple_(current.created_at, current.id),
            )
            .order_by(
                SubmissionCriterionVerdict.item_id,
                earlier.created_at.desc(),
                earlier.id.desc(),
            )
            .distinct(SubmissionCriterionVerdict.item_id)
        )
        result = await self._session.execute(stmt)
        return {row.item_id: row for row in result.scalars().all()}

    async def earlier_attempt(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> EarlierAttempt | None:
        """The attempt the submission's remarks are compared with (task 12a).

        The LATEST earlier attempt of the same student at the same task — a
        live submission that got a review (``ATTEMPT_STATUSES``, the attempt
        ledger's own notion) — at the same ``task_version``, that has verdict
        rows. So a copy of an unchanged work's review (``dedup_of``), which has
        none, is passed over for the attempt it repeated, and so is a review
        by today's Mentor; a submission still in flight is not an attempt,
        whatever rows it already has. "Earlier" is by creation time, then by
        id, as :meth:`previous_verdicts` orders.

        The criteria list is not part of the key, as it is for the safeguard:
        the items are matched by id, which an author's edit keeps (task 08).

        Returns:
            The attempt and its rows; ``None`` when there is nothing to
            compare with — a first attempt, none at this version of the task,
            or a submission whose version was never recorded (before the
            ledger, or the doors refused): an unknown version is never the
            same.
        """
        current = aliased(HomeworkSubmission)
        earlier = aliased(HomeworkSubmission)
        stmt = (
            select(earlier.id)
            .join(current, current.id == submission_id)
            .where(
                current.tenant_id == tenant_id,
                earlier.tenant_id == tenant_id,
                earlier.student_id == current.student_id,
                earlier.authored_document_id == current.authored_document_id,
                # NULL on either side matches nothing: an unknown version.
                earlier.task_version == current.task_version,
                earlier.status.in_(ATTEMPT_STATUSES),
                earlier.deleted_at.is_(None),
                tuple_(earlier.created_at, earlier.id)
                < tuple_(current.created_at, current.id),
                exists().where(
                    SubmissionCriterionVerdict.tenant_id == tenant_id,
                    SubmissionCriterionVerdict.submission_id == earlier.id,
                ),
            )
            .order_by(earlier.created_at.desc(), earlier.id.desc())
            .limit(1)
        )
        earlier_id = await self._session.scalar(stmt)
        if earlier_id is None:
            return None
        rows = await self.list_for_submission(
            tenant_id=tenant_id, submission_id=earlier_id
        )
        return EarlierAttempt(submission_id=earlier_id, rows=tuple(rows))

    async def store_explanation(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        language: str,
        body: dict[str, Any],
    ) -> SubmissionExplanation:
        """Keep the explanation stage's answer, replacing an earlier one.

        The replacement is bounded by the tenant as well: a row of another
        tenant is never overwritten, and the call then fails instead of
        returning a row that is not the caller's.
        """
        stmt = (
            pg_insert(SubmissionExplanation)
            .values(
                tenant_id=tenant_id,
                submission_id=submission_id,
                language=language,
                body=body,
            )
            .on_conflict_do_update(
                index_elements=[SubmissionExplanation.submission_id],
                set_={"language": language, "body": body, "updated_at": func.now()},
                where=SubmissionExplanation.tenant_id == tenant_id,
            )
            .returning(SubmissionExplanation)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def get_explanation(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> SubmissionExplanation | None:
        """The explanation stage's answer for the submission, or ``None``."""
        stmt = select(SubmissionExplanation).where(
            SubmissionExplanation.tenant_id == tenant_id,
            SubmissionExplanation.submission_id == submission_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def store_candidates(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        body: dict[str, Any],
    ) -> SubmissionCourseLinkCandidates:
        """Keep the course links found for the submission, replacing earlier ones.

        Bounded by the tenant as :meth:`store_explanation` is: a row of another
        tenant is never overwritten, and the call then fails instead of
        returning a row that is not the caller's.
        """
        stmt = (
            pg_insert(SubmissionCourseLinkCandidates)
            .values(tenant_id=tenant_id, submission_id=submission_id, body=body)
            .on_conflict_do_update(
                index_elements=[SubmissionCourseLinkCandidates.submission_id],
                set_={"body": body, "updated_at": func.now()},
                where=SubmissionCourseLinkCandidates.tenant_id == tenant_id,
            )
            .returning(SubmissionCourseLinkCandidates)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def get_candidates(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> SubmissionCourseLinkCandidates | None:
        """The course links found for the submission, or ``None``."""
        stmt = select(SubmissionCourseLinkCandidates).where(
            SubmissionCourseLinkCandidates.tenant_id == tenant_id,
            SubmissionCourseLinkCandidates.submission_id == submission_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


def _row(
    record: VerdictRecord,
    *,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
    criteria_layer: CriteriaLayer,
    criteria_source_id: uuid.UUID,
) -> SubmissionCriterionVerdict:
    return SubmissionCriterionVerdict(
        tenant_id=tenant_id,
        submission_id=submission_id,
        criteria_layer=criteria_layer.value,
        criteria_source_id=criteria_source_id,
        item_id=record.item_id,
        item_kind=record.item_kind.value,
        weight=record.weight.value if record.weight else None,
        verdict=record.verdict.value,
        model_verdict=record.model_verdict.value if record.model_verdict else None,
        quote=record.quote,
        quote_file=record.quote_file,
        quote_line_start=record.quote_line_start,
        quote_line_end=record.quote_line_end,
        missing=record.missing,
        retried=record.retried,
        repeat_reason=record.repeat_reason.value if record.repeat_reason else None,
        quote_not_found=record.quote_not_found,
        quote_from_base=record.quote_from_base,
        safeguard_fired=record.safeguard_submission_id is not None,
        safeguard_submission_id=record.safeguard_submission_id,
        first_model_verdict=(
            record.first_model_verdict.value if record.first_model_verdict else None
        ),
        answered_by_call_id=record.answered_by_call_id,
        remark_state=record.remark_state.value if record.remark_state else None,
    )
