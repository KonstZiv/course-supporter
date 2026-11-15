"""Submissions held for funds, continued once their payer can pay (BIL-E, A5).

Purpose:
    A review whose payer could not cover its price when its run started is
    held in ``awaiting_funds`` (the portal shows the code ``awaiting_funds``);
    nothing about it is lost — its file, its checkpoint and its place stay.
    Once the payer has the units, :func:`resume_funded` gives it a new job
    through ``path_continuation.resume_after_top_up`` — the seam the path
    keeps for exactly this — and the run asks the funds port again: a top-up
    that was not enough holds it again, from ``received``.

When the payer can pay:
    Its available units cover the review's price P — the one its run was
    refused with, from the review's estimate row (``action_cost_estimates``,
    key: the submission); without a row, the typical price of the task
    (:func:`.quotes.review_quote`). A test needs a balance above 0. A review
    an author pays for under a rule is never held, so it is continued at once.

Idempotent: a submission with a job in flight is skipped
(``start_continuation_job``), and a continued one leaves ``awaiting_funds``
when its run starts. Nothing happens while charging is off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import select

from course_supporter.billing import charging, ledger
from course_supporter.billing.kinds import AccountKind, ActionKind
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

BATCH: Final = 200
"""Held submissions looked at per run, oldest first."""


@dataclass(frozen=True, slots=True)
class Resumed:
    """What one run did."""

    looked_at: int
    resumed: list[uuid.UUID]


async def _price(
    session: AsyncSession, submission: HomeworkSubmission, task: AuthoredDocument
) -> int:
    """P the review was refused with (its estimate row), else the typical P."""
    from course_supporter.billing.quotes import review_quote

    p_units = await session.scalar(
        select(ActionCostEstimate.p_units).where(
            ActionCostEstimate.action_kind == ActionKind.REVIEW.value,
            ActionCostEstimate.action_key == str(submission.id),
        )
    )
    if p_units is not None:
        return int(p_units)
    quote = await review_quote(
        session,
        task=task,
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
    )
    return quote.price_units


async def can_pay(session: AsyncSession, submission: HomeworkSubmission) -> bool:
    """Can the review's payer cover it now (module docstring)?"""
    task = await session.get(AuthoredDocument, submission.authored_document_id)
    if task is None:
        return False
    payer = await charging.payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=submission.tenant_id,
        course_node_id=task.course_root_id,
        student_id=submission.student_id,
    )
    if payer.kind is AccountKind.AUTHOR:
        return True
    if task.task_type == AssignmentType.TEST.value:
        return await charging.test_allowed(session, payer)
    available = (await ledger.balance(session, payer.account_id)).available
    return available >= await _price(session, submission, task)


async def resume_funded(session: AsyncSession, arq: Any, *, dry_run: bool) -> Resumed:
    """Continue every held review whose payer can pay now; oldest first.

    ``dry_run`` counts and dispatches nothing. Commits: each continuation
    commits its job before dispatching it (``start_continuation_job``).
    """
    from course_supporter.homework.path_continuation import resume_after_top_up

    held = list(
        (
            await session.execute(
                select(HomeworkSubmission)
                .where(HomeworkSubmission.status == HomeworkStatus.AWAITING_FUNDS.value)
                .order_by(HomeworkSubmission.created_at, HomeworkSubmission.id)
                .limit(BATCH)
            )
        ).scalars()
    )
    resumed: list[uuid.UUID] = []
    for submission in held:
        if not await can_pay(session, submission):
            continue
        if dry_run or await resume_after_top_up(session, arq, submission.id):
            resumed.append(submission.id)
            logger.info("billing_resume_funded", submission_id=str(submission.id))
    await session.commit()  # accounts are created lazily on the way
    return Resumed(looked_at=len(held), resumed=resumed)
