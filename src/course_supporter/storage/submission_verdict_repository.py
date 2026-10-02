"""Reads and writes of a submission's verdicts on criteria (mentor-rebuild task 09b).

Purpose:
    The evaluation stage writes a submission's verdicts; the explanation stage
    and the review's builder read them; the next submission of the same student
    reads them back through the resubmission safeguard. Each of these has to
    remember the same filters — the tenant, and the live submission — and a
    rule spread over several call sites is a rule that drifts, so all of them
    live here, with the explanation stage's answer beside them.

Interface:
    :class:`VerdictRecord` — one verdict as a stage hands it in.
    :class:`SubmissionVerdictRepository` — five methods: ``replace_for_submission``,
    ``list_for_submission``, ``previous_verdicts``, ``store_explanation`` and
    ``get_explanation``.

    Writes ``flush`` and never commit: the caller owns the transaction, so a
    stage's verdicts are committed together with the checkpoint that marks the
    stage done (``homework/path_runner.py``).

Replacing:
    The stages depend on these five methods and on the two value types only;
    another store implements the same five without touching a stage.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.storage.orm import (
    HomeworkSubmission,
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
            not found.
        quote_file: Where the quote was found; ``None`` when it was not.
        quote_lines: First and last line of the quote in ``quote_file``;
            ``None`` for a document, whose lines are an artefact of extraction.
        missing: The model's sentence on what is missing.
        retried: The item was asked about once more.
        quote_not_found: The model's quote was not in the work after the
            repeat.
        safeguard_submission_id: The earlier submission whose "met" the
            safeguard kept; ``None`` when it did not fire.
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
    quote_not_found: bool = False
    safeguard_submission_id: uuid.UUID | None = None


class SubmissionVerdictRepository:
    """Storage for a submission's verdicts and for its explanation."""

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


def _row(
    record: VerdictRecord,
    *,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
    criteria_layer: CriteriaLayer,
    criteria_source_id: uuid.UUID,
) -> SubmissionCriterionVerdict:
    first, last = record.quote_lines if record.quote_lines else (None, None)
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
        quote_line_start=first,
        quote_line_end=last,
        missing=record.missing,
        retried=record.retried,
        quote_not_found=record.quote_not_found,
        safeguard_fired=record.safeguard_submission_id is not None,
        safeguard_submission_id=record.safeguard_submission_id,
    )
