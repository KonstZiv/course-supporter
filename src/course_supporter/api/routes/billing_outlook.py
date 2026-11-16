"""What the reminders read (BIL-E, part C).

Routes
------
- ``GET /portal/billing/outlook`` — a student's session: about how many
  reviews their units cover, a warning when they run low, units that expire
  soon, and who pays. ``?course_id=`` names the course whose payment rule
  counts; without it, only a rule for the student does.
- ``GET /billing/outlook`` — an author (the tenant's key, scope ``prep``):
  about how many days the units last at the last week's pace, a warning when
  they run out soon, units that expire soon, and the students' reviews that
  wait for the author's units (``held_reviews``: the author pays for them
  under a payment rule and has too few — BIL-E2, 5a, item 2).

Screens show units only, and pick their words by the codes (``low``,
``next_may_fail``, ``runs_out_soon``). The thresholds are settings
(``BILLING_REMINDER_*``, ``BILLING_EXPIRY_NOTICE_DAYS``).

Both carry ``charging_enabled`` — the switch ``BILLING_CHARGING_ENABLED``
(:func:`course_supporter.billing.charging.charging_enabled`), the value and the
source of ``GET /billing/spending`` and of the estimate routes (BIL-E3). The
other fields do not depend on it: what to show while charging is off is the
screen's decision.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.billing import charging
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.billing.held import held_for_author
from course_supporter.billing.outlook import (
    Expiring,
    author_outlook,
    student_outlook,
)
from course_supporter.config import get_settings

router = APIRouter(tags=["billing"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]


_CHARGING_ENABLED = (
    "BILLING_CHARGING_ENABLED, as in GET /billing/spending and the estimate "
    "routes: while false nothing is charged or refused; the other fields are "
    "computed the same either way."
)


class ExpiringSoon(BaseModel):
    """Free units of the first batch that expires within the notice period."""

    units: int
    expires_on: date


class StudentOutlookResponse(BaseModel):
    reviews_left: int | None = Field(
        description="About how many reviews the available units cover; null "
        "when the author pays or nothing is known to price a review."
    )
    warning: Literal["low", "next_may_fail"] | None
    expiring_soon: ExpiringSoon | None
    paid_by: Literal["self", "author"]
    charging_enabled: bool = Field(description=_CHARGING_ENABLED)


class HeldReviewsOut(BaseModel):
    """Students' reviews that wait for the author's units."""

    count: int = Field(description="How many; 0 when none waits.")
    oldest_at: datetime | None = Field(
        description="When the oldest of them was submitted; null when none."
    )
    missing_units: int = Field(
        description="The prices of the waiting reviews together, less the "
        "available units; never below 0."
    )


class AuthorOutlookResponse(BaseModel):
    days_left: int | None = Field(
        description="About how many days the units last at the last week's "
        "pace; null without charges in that week."
    )
    warning: Literal["runs_out_soon"] | None
    expiring_soon: ExpiringSoon | None
    held_reviews: HeldReviewsOut = Field(
        description="Students' reviews the author pays for under a payment rule "
        "that wait for the author's units; they start, oldest first, once the "
        "units cover them."
    )
    charging_enabled: bool = Field(description=_CHARGING_ENABLED)


def _expiring(found: Expiring | None) -> ExpiringSoon | None:
    if found is None:
        return None
    return ExpiringSoon(units=found.units, expires_on=found.expires_on)


@router.get("/portal/billing/outlook", response_model=StudentOutlookResponse)
async def get_student_outlook(
    student: StudentDep,
    session: SessionDep,
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="The course (root) whose payment rule counts."),
    ] = None,
) -> StudentOutlookResponse:
    """The student's reminders: reviews left, a warning, units expiring soon."""
    found = await student_outlook(
        session,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
        settings=get_settings(),
        course_id=course_id,
    )
    await session.commit()  # accounts are created lazily on the way
    return StudentOutlookResponse(
        reviews_left=found.reviews_left,
        warning=found.warning,
        expiring_soon=_expiring(found.expiring_soon),
        paid_by=found.paid_by,
        charging_enabled=charging.charging_enabled(),
    )


@router.get("/billing/outlook", response_model=AuthorOutlookResponse)
async def get_author_outlook(
    tenant: PrepDep, session: SessionDep
) -> AuthorOutlookResponse:
    """The author's reminders: days left at the last week's pace, units expiring."""
    account = await get_or_create_author_account(session, tenant_id=tenant.tenant_id)
    found = await author_outlook(session, account.id, settings=get_settings())
    held = await held_for_author(
        session, tenant_id=tenant.tenant_id, account_id=account.id
    )
    await session.commit()
    return AuthorOutlookResponse(
        days_left=found.days_left,
        warning=found.warning,
        expiring_soon=_expiring(found.expiring_soon),
        held_reviews=HeldReviewsOut(
            count=held.count,
            oldest_at=held.oldest_at,
            missing_units=held.missing_units,
        ),
        charging_enabled=charging.charging_enabled(),
    )
