"""Submissions held for funds, continued once their payer can pay (BIL-E, A5).

Purpose:
    A review whose payer could not cover its price when its run started is
    held in ``awaiting_funds`` (the portal shows the code ``awaiting_funds``,
    or ``awaiting_author_funds`` when the author pays — :mod:`.held`);
    nothing about it is lost — its file, its checkpoint and its place stay.
    Once the payer has the units, :func:`resume_funded` gives it a new job
    through ``path_continuation.resume_after_top_up`` — the seam the path
    keeps for exactly this — and the run asks the funds port again: a top-up
    that was not enough holds it again, from ``received``.

When the payer can pay:
    The payer is the review's payer today (:func:`.held.payer_of_held`) — the
    student, or the author under a payment rule (BIL-E2, 5a, item 2: a review the
    author pays for waits for the author's units like a student's for theirs).
    Its available units cover the review's price P — the one its run was
    refused with, from the review's estimate row (``action_cost_estimates``,
    key: the submission); without a row, the threshold of a review priced at
    its ceiling — the task's typical price, else ``available > 0``
    (:func:`.held.price_of_held`, :func:`.quotes.ceiling_threshold`). A test
    needs a balance above 0. An author with credit is never waited for: their
    reviews are continued at once.

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

from course_supporter.billing import charging
from course_supporter.billing.held import payer_of_held, price_of_held
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
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


async def can_pay(session: AsyncSession, submission: HomeworkSubmission) -> bool:
    """Can the review's payer cover it now (module docstring)?"""
    task = await session.get(AuthoredDocument, submission.authored_document_id)
    if task is None:
        return False
    payer = await payer_of_held(session, submission, task)
    if payer is None:
        return False
    if payer.credit_allowed:
        return True
    if task.task_type == AssignmentType.TEST.value:
        return await charging.test_allowed(session, payer)
    available = await charging.available_units(session, payer.account_id)
    return available >= await price_of_held(session, submission, task)


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
