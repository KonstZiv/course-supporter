"""What the students paid for the reviews — the one place the money is counted.

Read-only. The administrator's cost report (:mod:`.costs`, BIL-COSTS Б-2,
operator 2026-11-24) and the author's students' money (:mod:`.students_money`,
Б-1, operator 2026-11-26) both count it here, so their numbers cannot drift
apart.

The rules:

- A review is counted once when it has a ``debit`` on a student's own account
  in the period (a review the author pays for under a rule has its debit on the
  author's account and is not here). ``units`` of a row are those debits.
- A debit taken from a PAID batch (a purchase not marked in
  ``billing_test_batches``) is worth ``units * amount_paid_minor / batch.units``
  in the batch's currency; a row sums the exact shares per currency and rounds
  ONCE, half up (a totals row is rounded on its own, not summed from rounded
  rows). A debit from a batch without money (an admin credit, or a marked test
  purchase) is ``gift_units``. Refunds (``billing_refunds``) are neither
  subtracted from the money nor counted in ``gift_units``.
- A service student (``students.is_test_student``) is left out of all of it
  unless ``include_test_students``; their reviews are counted apart
  (:func:`excluded_test_reviews`).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, func, select

from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    BatchSource,
    MovementKind,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingBatch,
    BillingMovement,
    BillingTestBatch,
    Student,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ReviewFigures:
    """What students paid for the reviews of a row (module docstring).

    ``count`` — different reviews with a ``debit`` on a student's account;
    ``units_per_review`` — the row's ``students_units`` ÷ ``count`` rounded up,
    ``None`` when there are no reviews; ``money_minor`` — per currency (sorted
    by code), whole minor units of the paid batches the debits were taken
    from; ``gift_units`` — debited from batches without money.
    """

    count: int = 0
    units_per_review: int | None = None
    money_minor: tuple[tuple[str, int], ...] = ()
    gift_units: int = 0


@dataclass(frozen=True, slots=True)
class ReviewDebit:
    """Debits of one review from one batch: its money, or its gift units.

    ``units`` — all the units debited (money or gift); ``student_id`` — the
    student whose own account was debited; ``batch_id`` — the batch they were
    taken from (the partner's report takes its tax and fee from it, Б-3).
    """

    tenant_id: uuid.UUID
    student_id: uuid.UUID | None
    action_key: str
    units: int
    gift_units: int
    currency: str | None
    money: Fraction
    batch_id: uuid.UUID


class ReviewTally:
    """The exact sums behind a :class:`ReviewFigures`, kept until rounding."""

    __slots__ = ("gift_units", "keys", "money", "units")

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.units = 0
        self.gift_units = 0
        self.money: dict[str, Fraction] = {}

    def add(self, debit: ReviewDebit) -> None:
        self.keys.add(debit.action_key)
        self.units += debit.units
        self.gift_units += debit.gift_units
        if debit.currency is not None:
            self.money[debit.currency] = (
                self.money.get(debit.currency, Fraction(0)) + debit.money
            )

    def merge(self, other: ReviewTally) -> None:
        self.keys |= other.keys
        self.units += other.units
        self.gift_units += other.gift_units
        for currency, value in other.money.items():
            self.money[currency] = self.money.get(currency, Fraction(0)) + value

    def money_minor(self) -> tuple[tuple[str, int], ...]:
        """Per currency (sorted by code), rounded once, half up."""
        return tuple(
            (currency, math.floor(value + Fraction(1, 2)))
            for currency, value in sorted(self.money.items())
        )

    def figures(self, students_units: int) -> ReviewFigures:
        """Round once, half up; ``units_per_review`` from the row's students' units."""
        count = len(self.keys)
        return ReviewFigures(
            count=count,
            units_per_review=-(-students_units // count) if count else None,
            money_minor=self.money_minor(),
            gift_units=self.gift_units,
        )


def tally(debits: list[ReviewDebit]) -> ReviewTally:
    """The sums of some debits."""
    total = ReviewTally()
    for debit in debits:
        total.add(debit)
    return total


def student_review_debits() -> tuple[ColumnElement[bool], ...]:
    """Debits for a review on a student's own account of a tenant."""
    return (
        BillingMovement.kind == MovementKind.DEBIT.value,
        BillingMovement.action_kind == ActionKind.REVIEW.value,
        BillingMovement.action_key.is_not(None),
        BillingAccount.kind == AccountKind.STUDENT.value,
        BillingAccount.tenant_id.is_not(None),
    )


async def review_debits(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
    include_test_students: bool,
) -> list[ReviewDebit]:
    """Each review's debits, by the student and the batch they were taken from.

    A paid batch (a purchase not marked as a test one) gives money, the
    debit's share of what the batch cost: ``units * amount_paid_minor /
    batch.units``, exact; any other batch (an admin credit, a marked test
    purchase) gives gift units. ``tenant_id`` ``None`` — every tenant (the
    administrator's report); the author's routes always pass theirs.
    """
    marked = BillingTestBatch.batch_id.is_not(None)
    stmt = (
        select(
            BillingAccount.tenant_id,
            BillingAccount.student_id,
            BillingMovement.action_key,
            BillingBatch.id,
            BillingBatch.source,
            BillingBatch.units,
            BillingBatch.amount_paid_minor,
            BillingBatch.currency,
            marked.label("marked"),
            func.sum(BillingMovement.units),
        )
        .select_from(BillingMovement)
        .join(BillingAccount, BillingAccount.id == BillingMovement.account_id)
        .join(BillingBatch, BillingBatch.id == BillingMovement.batch_id)
        .outerjoin(BillingTestBatch, BillingTestBatch.batch_id == BillingBatch.id)
        .outerjoin(Student, Student.id == BillingAccount.student_id)
        .where(
            *student_review_debits(),
            BillingMovement.created_at >= start_dt,
            BillingMovement.created_at < end_dt_exclusive,
        )
        .group_by(
            BillingAccount.tenant_id,
            BillingAccount.student_id,
            BillingMovement.action_key,
            BillingBatch.id,
            BillingBatch.source,
            BillingBatch.units,
            BillingBatch.amount_paid_minor,
            BillingBatch.currency,
            marked,
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(BillingAccount.tenant_id == tenant_id)
    if not include_test_students:
        stmt = stmt.where(Student.is_test_student.is_not(True))

    found: list[ReviewDebit] = []
    rows = (await session.execute(stmt)).tuples()
    for (
        t_id,
        s_id,
        key,
        b_id,
        source,
        batch_units,
        paid,
        currency,
        is_marked,
        units,
    ) in rows:
        if t_id is None or key is None:
            continue
        free = (
            source != BatchSource.PURCHASE.value
            or is_marked
            or paid is None
            or currency is None
        )
        found.append(
            ReviewDebit(
                tenant_id=t_id,
                student_id=s_id,
                action_key=key,
                units=int(units),
                gift_units=int(units) if free else 0,
                currency=None if free else currency,
                money=(
                    Fraction(0)
                    if free or paid is None
                    else Fraction(int(units) * int(paid), int(batch_units))
                ),
                batch_id=b_id,
            )
        )
    return found


async def excluded_test_reviews(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> int:
    """Different reviews of test students with a debit in the period."""
    stmt = (
        select(func.count(func.distinct(BillingMovement.action_key)))
        .join(BillingAccount, BillingAccount.id == BillingMovement.account_id)
        .join(Student, Student.id == BillingAccount.student_id)
        .where(
            *student_review_debits(),
            Student.is_test_student.is_(True),
            BillingMovement.created_at >= start_dt,
            BillingMovement.created_at < end_dt_exclusive,
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(BillingAccount.tenant_id == tenant_id)
    return int((await session.execute(stmt)).scalar_one())
