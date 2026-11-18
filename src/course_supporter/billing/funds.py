"""Charging wired into the actions (BIL-E): the journal's funds port and the gates.

Purpose:
    Where each paid action meets :mod:`.charging`:

    * :class:`LedgerFundsPort` — the funds port (``course_supporter.funds_port``)
      on the journal: a student's review on the new path and a task's key
      explanations. Chosen by :func:`course_supporter.funds_port.build_funds_port`
      while charging is possible (:func:`.charging.charging_possible`).
    * :func:`processing_started` / :func:`job_ended` — a material's processing:
      the threshold and the reserve at its shadow estimate, before its first
      paid step (operator 2026-11-15, S1); its fact or its release when the job
      ends.
    * :func:`preparation_started` / :func:`preparation_ended` — the criteria
      decomposition a submission runs; never waits for the author's units.
    * :func:`billing_note` — what a student is told about a review that cost
      more than its reserve (notification 6.3).

    Every function here is a no-op while charging is off, and for a payer it
    does not apply to (:func:`.charging.charging_applies`): nothing is written
    to the journal or the audit, no account is created, nothing is refused (A6).

Who pays:
    Always :func:`.payer.resolve_payer`, through :func:`.charging.charged_payer_of`
    — the student for their own review, the author for processing and task
    preparation, the author under a payment rule for a student's review. It
    reads the payer first and resolves it only when charging applies to it.

Transactions:
    Each entry opens its own session from the factory and commits: a reserve
    and a charge are durable whatever the action's own transaction does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import select

from course_supporter.billing import charging
from course_supporter.billing.estimates import preparation_e_usd, processing_e_usd
from course_supporter.billing.kinds import AccountKind, ActionKind, MovementKind
from course_supporter.billing.quotes import NOTHING_PRICED_THRESHOLD, ceiling_threshold
from course_supporter.cost_estimate.shadow import (
    preparation_key,
    processing_key,
    review_key,
)
from course_supporter.funds_port import (
    FundsAnswer,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
)
from course_supporter.homework.path_config import in_test_mode
from course_supporter.models.source import AssignmentType
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingMovement,
    CourseNode,
    HomeworkSubmission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.cost_estimate.actions import MaterialFactors

logger = structlog.get_logger(__name__)

_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure(session_factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Give the worker's session factory to the gates that hold none."""
    global _session_factory
    _session_factory = session_factory


async def _tenant_of_document(
    session: AsyncSession, document: AuthoredDocument
) -> uuid.UUID | None:
    """The tenant that owns the task, through its course node."""
    node = await session.get(CourseNode, document.course_node_id)
    return node.tenant_id if node is not None else None


async def _task_of_submission(
    session: AsyncSession, submission_id: uuid.UUID
) -> AuthoredDocument | None:
    return (
        await session.execute(
            select(AuthoredDocument)
            .join(
                HomeworkSubmission,
                HomeworkSubmission.authored_document_id == AuthoredDocument.id,
            )
            .where(HomeworkSubmission.id == submission_id)
        )
    ).scalar_one_or_none()


# ── The funds port on the journal ─────────────────────────────────────


class LedgerFundsPort:
    """The funds port of BIL-E: reserve at the start, the fact at the end.

    A review: on the path's test mode (``path_config.in_test_mode``) nothing is
    reserved or charged (operator 2026-11-03, item 4); a test (kind ``test``,
    no model) is free while its payer's balance is above 0; any other review
    is gated by :func:`.charging.threshold_applies` — a review an author
    without credit pays for under a rule too: short, it is held in
    ``awaiting_funds`` like a student's (BIL-E2, 5a, item 2) — reserves
    ``min(2E, the path's ceiling, available)`` and is settled, at its end, with
    the cost of its calls (:func:`.charging.review_fact_usd`). A review whose
    estimate fell to the ceiling (``expected_usd`` is ``None``) needs the
    task's typical price to start, else ``available > 0``
    (:func:`.quotes.ceiling_threshold`), and reserves ``min(N, available)``,
    N the ceiling in units. A failure releases everything.
    The numbers of every answer are also kept on the funds-port row of the
    call register, as by the first implementation. A payer charging does not
    apply to (:func:`.charging.charged_payer_of`) is answered as by
    :class:`~course_supporter.funds_port.AlwaysEnoughFundsPort`: allowed,
    nothing held, nothing created; its end then finds no reserve and writes
    nothing.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _record(
        self, path_key: Any, ceiling_estimate_usd: float, answer: FundsAnswer
    ) -> None:
        from course_supporter.service_logging import record_funds_decision

        await record_funds_decision(
            self._session_factory,
            path_key=path_key,
            ceiling_estimate_usd=ceiling_estimate_usd,
            answer=answer,
        )

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """The threshold and the reserve of a review, at its start."""
        answer = await self._start_review(context, ceiling_estimate_usd)
        await self._record(context.path_key, ceiling_estimate_usd, answer)
        return answer

    async def _start_review(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        task_type = context.path_key.task_type
        if in_test_mode(task_type):
            return FundsAnswer.allowed()
        ceiling = Decimal(str(ceiling_estimate_usd))
        at_ceiling = context.expected_usd is None
        e_usd = ceiling if context.expected_usd is None else context.expected_usd
        async with self._session_factory() as session:
            task = await _task_of_submission(session, context.submission_id)
            payer = await charging.charged_payer_of(
                session,
                action_kind=ActionKind.REVIEW,
                tenant_id=context.tenant_id,
                course_node_id=task.course_root_id if task is not None else None,
                student_id=context.student_id,
            )
            if payer is None:  # as AlwaysEnoughFundsPort: nothing held
                return FundsAnswer.allowed()
            if task_type is AssignmentType.TEST:
                allowed = await charging.test_allowed(session, payer)
                await session.commit()
                return (
                    FundsAnswer.allowed()
                    if allowed
                    else FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)
                )
            threshold = charging.price_of(e_usd)
            if at_ceiling and charging.threshold_applies(
                ActionKind.REVIEW, payer, own_action=False
            ):
                # The estimate fell to the ceiling: the start needs the task's
                # typical price (else available > 0); the reserve below is
                # min(N, available), the excess over it the platform's.
                threshold = (
                    await ceiling_threshold(
                        session,
                        task=task,
                        tenant_id=context.tenant_id,
                        student_id=context.student_id,
                    )
                    if task is not None
                    else NOTHING_PRICED_THRESHOLD
                )
            found = await charging.quote(
                session,
                action_kind=ActionKind.REVIEW,
                payer=payer,
                price_units=threshold,
                lock=True,
            )
            if not found.enough:
                await session.commit()
                logger.info(
                    "billing_review_refused",
                    submission_id=str(context.submission_id),
                    price_units=found.price_units,
                    available_units=found.available_units,
                    paid_by="author" if payer.for_someone_else else "self",
                )
                return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)
            await charging.reserve_for(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=review_key(context.submission_id),
                payer=payer,
                e_usd=e_usd,
                ceiling_usd=ceiling,
            )
            await session.commit()
        return FundsAnswer.allowed()

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        """Nothing per stage: the fact is read from the register at the end."""

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        """Completed: settle the review's fact. Failed: release its reserve."""
        if in_test_mode(context.path_key.task_type):
            return
        key = review_key(context.submission_id)
        async with self._session_factory() as session:
            if outcome is SubmissionOutcome.FAILED:
                await charging.fail(
                    session, action_kind=ActionKind.REVIEW, action_key=key
                )
            else:
                task = await _task_of_submission(session, context.submission_id)
                await charging.finish(
                    session,
                    action_kind=ActionKind.REVIEW,
                    action_key=key,
                    fact_usd=await charging.review_fact_usd(
                        session, context.submission_id
                    ),
                    tenant_id=context.tenant_id,
                    course_node_id=task.course_root_id if task is not None else None,
                    student_id=context.student_id,
                )
            await session.commit()

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """The threshold and the reserve of a task's key explanations.

        The threshold is the author's own action's: only when the author asked
        (not a student's submission) and has no credit.
        """
        answer = await self._start_version(context, ceiling_estimate_usd)
        await self._record(None, ceiling_estimate_usd, answer)
        return answer

    def _version_key(self, context: VersionWorkContext) -> str:
        version = context.reference_id or context.source_content_hash
        return preparation_key(
            context.authored_document_id, str(version), context.work_kind.value
        )

    async def _start_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        ceiling = Decimal(str(ceiling_estimate_usd))
        e_usd = context.expected_usd if context.expected_usd is not None else ceiling
        async with self._session_factory() as session:
            task = await session.get(AuthoredDocument, context.authored_document_id)
            payer = await charging.charged_payer_of(
                session,
                action_kind=ActionKind.TASK_PREPARATION,
                tenant_id=context.tenant_id,
                course_node_id=task.course_root_id if task is not None else None,
                student_id=None,
            )
            if payer is None:  # as AlwaysEnoughFundsPort: nothing held
                return FundsAnswer.allowed()
            found = await charging.quote(
                session,
                action_kind=ActionKind.TASK_PREPARATION,
                payer=payer,
                price_units=charging.price_of(e_usd),
                own_action=not context.requested_by_submission,
                lock=True,
            )
            if not found.enough:
                await session.commit()
                return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)
            await charging.reserve_for(
                session,
                action_kind=ActionKind.TASK_PREPARATION,
                action_key=self._version_key(context),
                payer=payer,
                e_usd=e_usd,
                ceiling_usd=ceiling,
            )
            await session.commit()
        return FundsAnswer.allowed()

    async def account_version_work_cost(
        self,
        context: VersionWorkContext,
        actual_usd: float,
        outcome: SubmissionOutcome = SubmissionOutcome.COMPLETED,
    ) -> None:
        """Settle the work's fact, or release everything when it failed."""
        key = self._version_key(context)
        async with self._session_factory() as session:
            if outcome is SubmissionOutcome.FAILED:
                await charging.fail(
                    session, action_kind=ActionKind.TASK_PREPARATION, action_key=key
                )
            else:
                task = await session.get(AuthoredDocument, context.authored_document_id)
                await charging.finish(
                    session,
                    action_kind=ActionKind.TASK_PREPARATION,
                    action_key=key,
                    fact_usd=Decimal(str(actual_usd)),
                    tenant_id=context.tenant_id,
                    course_node_id=task.course_root_id if task is not None else None,
                    student_id=None,
                )
            await session.commit()


# ── Processing of a material ──────────────────────────────────────────


class InsufficientUnitsError(Exception):
    """An author's own action cannot start: the payer is short and has no credit.

    Carries :attr:`category` (``insufficient_units``) for the execution seam,
    which writes it to ``jobs.error_category``; the ingestion callback writes
    the same to ``authored_documents.error_category``, where the panel reads it.
    """

    category: Final = ErrorCategory.INSUFFICIENT_UNITS

    def __init__(self, *, price_units: int, available_units: int) -> None:
        super().__init__(
            f"insufficient_units: the price is {price_units} units, "
            f"{available_units} are available"
        )
        self.price_units = price_units
        self.available_units = available_units


async def processing_started(
    *,
    kind: str,
    factors: MaterialFactors,
    job_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    authored_document_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """The threshold and the reserve of a processing job, before its first paid step.

    Called at the shadow estimate of the job (S1). An author without credit
    who cannot cover P: :class:`InsufficientUnitsError` — the job fails before
    any paid call. With credit, or covered: a reserve of
    ``min(2E, available)``. No estimate: nothing is reserved (S2).

    Raises:
        InsufficientUnitsError: see above.
    """
    factory = session_factory or _session_factory
    if not charging.charging_possible() or factory is None:
        return
    if job_id is None or tenant_id is None:
        logger.warning("billing_processing_without_job", kind=kind)
        return
    e_usd = processing_e_usd(kind=kind, factors=factors)
    if e_usd is None:
        return
    async with factory() as session:
        task = (
            await session.get(AuthoredDocument, authored_document_id)
            if authored_document_id is not None
            else None
        )
        payer = await charging.charged_payer_of(
            session,
            action_kind=ActionKind.PROCESSING,
            tenant_id=tenant_id,
            course_node_id=task.course_root_id if task is not None else None,
            student_id=None,
        )
        if payer is None:
            return
        found = await charging.quote(
            session,
            action_kind=ActionKind.PROCESSING,
            payer=payer,
            price_units=charging.price_of(e_usd),
            own_action=True,
            lock=True,
        )
        if not found.enough:
            await session.commit()
            logger.info(
                "billing_processing_refused",
                job_id=str(job_id),
                price_units=found.price_units,
                available_units=found.available_units,
            )
            raise InsufficientUnitsError(
                price_units=found.price_units, available_units=found.available_units
            )
        await charging.reserve_for(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=processing_key(job_id),
            payer=payer,
            e_usd=e_usd,
            ceiling_usd=None,
        )
        await session.commit()


async def job_ended(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    completed: bool,
) -> None:
    """A processing job ended: settle its calls' cost, or release its reserve.

    A job that reserved nothing (charging off at its start, no estimate) is
    left alone. Never raises: the job's end is already written.
    """
    if not charging.charging_possible():
        return
    key = processing_key(job_id)
    try:
        async with session_factory() as session:
            if not completed:
                await charging.fail(
                    session, action_kind=ActionKind.PROCESSING, action_key=key
                )
            else:
                task = await session.get(AuthoredDocument, authored_document_id)
                tenant_id = (
                    await _tenant_of_document(session, task)
                    if task is not None
                    else None
                )
                if task is not None and tenant_id is not None:
                    await charging.finish(
                        session,
                        action_kind=ActionKind.PROCESSING,
                        action_key=key,
                        fact_usd=await charging.job_fact_usd(session, job_id),
                        tenant_id=tenant_id,
                        course_node_id=task.course_root_id,
                        student_id=None,
                    )
            await session.commit()
    except Exception:
        logger.exception("billing_processing_end_failed", job_id=str(job_id))


# ── A task preparation a submission runs (criteria decomposition) ─────


@dataclass(frozen=True, slots=True)
class PreparationCharge:
    """A started preparation: what its end needs."""

    action_key: str
    tenant_id: uuid.UUID
    course_node_id: uuid.UUID | None


async def preparation_started(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    document: AuthoredDocument,
    kind: str,
    version: str,
    task_chars: int,
) -> PreparationCharge | None:
    """Reserve for a preparation a student's submission started.

    No threshold, no refusal (operator 2026-11-15): the reserve is
    ``min(2E, available)`` and may be 0. ``None`` while charging is off, does
    not apply to the payer, or no estimate can be made. Never raises: a review
    is never stopped by it.
    """
    if not charging.charging_possible():
        return None
    e_usd = preparation_e_usd(kind=kind, task_chars=task_chars)
    if e_usd is None:
        return None
    action_key = preparation_key(document.id, version, kind)
    try:
        async with session_factory() as session:
            tenant_id = await _tenant_of_document(session, document)
            if tenant_id is None:
                return None
            charge = PreparationCharge(
                action_key=action_key,
                tenant_id=tenant_id,
                course_node_id=document.course_root_id,
            )
            payer = await charging.charged_payer_of(
                session,
                action_kind=ActionKind.TASK_PREPARATION,
                tenant_id=charge.tenant_id,
                course_node_id=charge.course_node_id,
                student_id=None,
            )
            if payer is None:
                return None
            await charging.reserve_for(
                session,
                action_kind=ActionKind.TASK_PREPARATION,
                action_key=charge.action_key,
                payer=payer,
                e_usd=e_usd,
                ceiling_usd=None,
            )
            await session.commit()
    except Exception:
        logger.exception("billing_preparation_start_failed", key=action_key)
        return None
    return charge


async def preparation_ended(
    session_factory: async_sessionmaker[AsyncSession],
    charge: PreparationCharge | None,
    *,
    fact_usd: Decimal | None,
) -> None:
    """Settle the preparation's fact; ``fact_usd=None`` — it failed: release.

    Never raises.
    """
    if charge is None:
        return
    try:
        async with session_factory() as session:
            if fact_usd is None:
                await charging.fail(
                    session,
                    action_kind=ActionKind.TASK_PREPARATION,
                    action_key=charge.action_key,
                )
            else:
                await charging.finish(
                    session,
                    action_kind=ActionKind.TASK_PREPARATION,
                    action_key=charge.action_key,
                    fact_usd=fact_usd,
                    tenant_id=charge.tenant_id,
                    course_node_id=charge.course_node_id,
                    student_id=None,
                )
            await session.commit()
    except Exception:
        logger.exception("billing_preparation_end_failed", key=charge.action_key)


# ── What the student is told (notification 6.3) ───────────────────────

COST_OVER_RESERVE: Final = "cost_over_reserve"


async def billing_note(
    session: AsyncSession, submission_id: uuid.UUID
) -> dict[str, Any] | None:
    """``{kind, cost_units: F, charged_units: B}`` when the student's review
    cost more than its reserve and the platform took the rest; else ``None``.

    Only for a review the student paid for: what an author pays under a rule
    is not the student's to read.
    """
    reservation = await charging.reservation_of(
        session, ActionKind.REVIEW, review_key(submission_id)
    )
    if reservation is None or reservation.fact_units is None:
        return None
    rows = (
        await session.execute(
            select(BillingMovement.kind, BillingMovement.units).where(
                BillingMovement.account_id == reservation.account_id,
                BillingMovement.action_kind == ActionKind.REVIEW.value,
                BillingMovement.action_key == reservation.action_key,
            )
        )
    ).tuples()
    sums: dict[str, int] = {}
    for kind, units in rows:
        sums[kind] = sums.get(kind, 0) + units
    if not sums.get(MovementKind.PLATFORM_COVERED.value):
        return None
    from course_supporter.storage.orm import BillingAccount

    account = await session.get(BillingAccount, reservation.account_id)
    if account is None or account.kind != AccountKind.STUDENT.value:
        return None
    return {
        "kind": COST_OVER_RESERVE,
        "cost_units": reservation.fact_units,
        "charged_units": sums.get(MovementKind.DEBIT.value, 0),
    }


__all__ = [
    "COST_OVER_RESERVE",
    "InsufficientUnitsError",
    "LedgerFundsPort",
    "PreparationCharge",
    "billing_note",
    "configure",
    "job_ended",
    "preparation_ended",
    "preparation_started",
    "processing_started",
]
