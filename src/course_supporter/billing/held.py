"""Reviews held for units: whose units they wait for, and how many (BIL-E2).

Purpose:
    A review whose payer could not cover its price when its run started is
    held in ``awaiting_funds`` — its file, its checkpoint and its place stay
    (:mod:`.resume_funded` continues it). Who it waits for is its payer
    today, :func:`.charging.payer_of` — the one answer the run, the
    continuation, the portal and the author's panel share:

    * the student, for their own review — the portal's code
      ``awaiting_funds``;
    * the author, under a payment rule (operator 2026-11-15, 5a, item 2: the
      platform does not work on credit) — the portal's code
      ``awaiting_author_funds``, and a count in the author's outlook
      (:func:`held_for_author`).

    * :func:`payer_of_held` — the payer of one held review.
    * :func:`price_of_held` — the units its start needs.
    * :func:`waiting_for_author` — which of some submissions wait for the
      author's units.
    * :func:`held_for_author` — the held reviews an author pays for: how
      many, since when, how many units are missing.

Read-only, but accounts are created lazily on the way
(:func:`.payer.resolve_payer`): a caller that wants them kept commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from course_supporter.billing import charging, ledger
from course_supporter.billing.kinds import ActionKind
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TEST_NEED: Final = 1
"""The units a held test needs: a test is free while the balance is above 0."""


async def payer_of_held(
    session: AsyncSession,
    submission: HomeworkSubmission,
    task: AuthoredDocument | None = None,
) -> charging.Payer | None:
    """Who pays for the review of ``submission`` now; ``None`` without its task."""
    if task is None:
        task = await session.get(AuthoredDocument, submission.authored_document_id)
    if task is None:
        return None
    return await charging.payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=submission.tenant_id,
        course_node_id=task.course_root_id,
        student_id=submission.student_id,
    )


async def price_of_held(
    session: AsyncSession, submission: HomeworkSubmission, task: AuthoredDocument
) -> int:
    """The units the held review's start needs.

    P the run was refused with — its estimate row (``action_cost_estimates``,
    key: the submission); without a row, the typical price of the task
    (:func:`.quotes.review_price`, whoever pays). A test: :data:`TEST_NEED`.
    """
    from course_supporter.billing.quotes import review_price

    if task.task_type == AssignmentType.TEST.value:
        return TEST_NEED
    p_units = await session.scalar(
        select(ActionCostEstimate.p_units).where(
            ActionCostEstimate.action_kind == ActionKind.REVIEW.value,
            ActionCostEstimate.action_key == str(submission.id),
        )
    )
    if p_units is not None:
        return int(p_units)
    found = await review_price(
        session,
        task=task,
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
    )
    return found.units


async def waiting_for_author(
    session: AsyncSession, submissions: Iterable[HomeworkSubmission]
) -> frozenset[uuid.UUID]:
    """The held submissions among ``submissions`` that wait for the author's units."""
    found: set[uuid.UUID] = set()
    for submission in submissions:
        if submission.status != HomeworkStatus.AWAITING_FUNDS.value:
            continue
        payer = await payer_of_held(session, submission)
        if payer is not None and payer.for_someone_else:
            found.add(submission.id)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class HeldReviews:
    """Students' reviews that wait for one author's units."""

    count: int
    oldest_at: datetime | None
    missing_units: int
    """The prices of the held reviews together, less what is available; >= 0."""

    @classmethod
    def none(cls) -> HeldReviews:
        """No review waits."""
        return cls(count=0, oldest_at=None, missing_units=0)


async def held_for_author(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> HeldReviews:
    """The held reviews of the tenant's students the author's account pays for.

    Only the tenant's own submissions are read; a review held for its
    student's own units is not the author's.
    """
    held = list(
        (
            await session.execute(
                select(HomeworkSubmission, AuthoredDocument)
                .join(
                    AuthoredDocument,
                    AuthoredDocument.id == HomeworkSubmission.authored_document_id,
                )
                .where(
                    HomeworkSubmission.tenant_id == tenant_id,
                    HomeworkSubmission.status == HomeworkStatus.AWAITING_FUNDS.value,
                    HomeworkSubmission.deleted_at.is_(None),
                )
                .order_by(HomeworkSubmission.created_at, HomeworkSubmission.id)
            )
        ).tuples()
    )
    mine: list[tuple[HomeworkSubmission, AuthoredDocument]] = []
    credit = False
    for submission, task in held:
        payer = await payer_of_held(session, submission, task)
        if (
            payer is not None
            and payer.for_someone_else
            and payer.account_id == account_id
        ):
            mine.append((submission, task))
            credit = payer.credit_allowed
    if not mine:
        return HeldReviews.none()
    missing = 0
    if not credit:  # with credit they go at the next continuation, nothing missing
        needed = 0
        for submission, task in mine:
            needed += await price_of_held(session, submission, task)
        available = (await ledger.balance(session, account_id)).available
        missing = max(0, needed - available)
    return HeldReviews(
        count=len(mine), oldest_at=mine[0][0].created_at, missing_units=missing
    )
