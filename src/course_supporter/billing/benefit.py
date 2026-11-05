"""The author's benefit: what the platform covers, and the monthly limit (BIL-B).

Purpose:
    An author account may have a benefit (BIL-A ``billing_author_settings``):
    ``unlimited`` — the platform covers the author's actions; ``monthly_limit``
    — it covers them up to ``monthly_limit_units`` a calendar month (UTC).
    A covered action takes no units of the author: :func:`.ledger.settle`
    writes it as a ``benefit_covered`` movement, with its units, so that the
    limit's use is counted. Beyond the limit an action is charged as usual —
    split at the limit (operator 2026-11-05): with 30 left and a fact of 50,
    30 are covered and 20 charged.

What it covers (operator 2026-11-05):
    The author's OWN actions — processing and task preparation. A review is
    never covered, even when the author pays for it under a payment rule.
    Re-processing at the platform's initiative is not the benefit's: it is
    ``platform_covered`` and never counts against the limit.

The limit of a month, prorated by whole UTC days (operator 2026-11-05):

    limit of month = floor( Σ over its days d: limit_d ÷ days in the month )

    where ``limit_d`` is ``monthly_limit_units`` if the setting in force on day
    d is ``monthly_limit``, else 0. A day belongs to the setting in force at
    its END: the day a benefit is granted or changed counts for the NEW
    setting (two changes the same day — the last). Example: 300 a month,
    granted on 5 November → 300 x 26 / 30 = 260. The first month and a change
    in the middle of a month are the same rule.

    The use of the month counts the ``benefit_covered`` units of the days that
    count for the limit (the ``monthly_limit`` days, by the same rule); units
    covered on an ``unlimited`` day are not the limit's.

History:
    The settings in force on a day come from the audit log — every
    ``author_settings.update`` entry, its ``after`` and its ``created_at`` —
    never from the settings row, which knows only the present. Before the
    first entry an account has no benefit.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    AuditAction,
    Benefit,
    MovementKind,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingMovement,
)

COVERED_ACTIONS: frozenset[ActionKind] = frozenset(
    {ActionKind.PROCESSING, ActionKind.TASK_PREPARATION}
)
"""The actions a benefit covers (module docstring)."""

_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SettingsChange:
    """The benefit an author account had from ``at`` on."""

    at: datetime
    benefit: Benefit
    monthly_limit_units: int | None


@dataclass(frozen=True, slots=True)
class Allowance:
    """The limit of one month, its use and what is left."""

    month: date
    """The first day of the month."""
    benefit: Benefit
    """The setting in force at the moment of the reading."""
    limit_units: int
    """The month's limit, prorated (module docstring); 0 without monthly days."""
    used_units: int
    remaining_units: int
    """``max(0, limit_units - used_units)``."""


def month_start(at: date) -> datetime:
    """Midnight UTC of the first day of ``at``'s month."""
    return datetime(at.year, at.month, 1, tzinfo=UTC)


def _next_month(start: datetime) -> datetime:
    days = calendar.monthrange(start.year, start.month)[1]
    return start + days * _DAY


def setting_on(history: Sequence[SettingsChange], end: datetime) -> SettingsChange:
    """The setting in force just before ``end`` (none before the first change)."""
    found = SettingsChange(
        at=datetime.min.replace(tzinfo=UTC),
        benefit=Benefit.NONE,
        monthly_limit_units=None,
    )
    for change in history:
        if change.at >= end:
            break
        found = change
    return found


def _limit_of_day(change: SettingsChange) -> int:
    if change.benefit is Benefit.MONTHLY_LIMIT and change.monthly_limit_units:
        return change.monthly_limit_units
    return 0


def month_limit(history: Sequence[SettingsChange], month: date) -> int:
    """The prorated limit of ``month`` (module docstring), rounded down."""
    start = month_start(month)
    days = calendar.monthrange(start.year, start.month)[1]
    total = sum(
        _limit_of_day(setting_on(history, start + (day + 1) * _DAY))
        for day in range(days)
    )
    return total // days


def counts_for_limit(history: Sequence[SettingsChange], at: datetime) -> bool:
    """Does a unit covered at ``at`` count against the limit (its day's setting)?"""
    at = at.astimezone(UTC)
    day = datetime(at.year, at.month, at.day, tzinfo=UTC)
    return setting_on(history, day + _DAY).benefit is Benefit.MONTHLY_LIMIT


async def settings_history(
    session: AsyncSession, account_id: uuid.UUID, *, until: datetime
) -> list[SettingsChange]:
    """The account's benefit changes up to ``until``, oldest first."""
    rows = await session.execute(
        select(BillingAuditEntry.created_at, BillingAuditEntry.after)
        .where(
            BillingAuditEntry.action == AuditAction.AUTHOR_SETTINGS_UPDATE.value,
            BillingAuditEntry.entity_id == account_id,
            BillingAuditEntry.created_at <= until,
        )
        .order_by(BillingAuditEntry.created_at, BillingAuditEntry.id)
    )
    return [
        SettingsChange(
            at=at,
            benefit=Benefit(after["benefit"]),
            monthly_limit_units=after.get("monthly_limit_units"),
        )
        for at, after in rows.tuples()
        if after is not None
    ]


async def _used(
    session: AsyncSession,
    account_id: uuid.UUID,
    history: Sequence[SettingsChange],
    month: date,
    until: datetime,
) -> int:
    start = month_start(month)
    rows = await session.execute(
        select(BillingMovement.created_at, BillingMovement.units).where(
            BillingMovement.account_id == account_id,
            BillingMovement.kind == MovementKind.BENEFIT_COVERED.value,
            BillingMovement.created_at >= start,
            BillingMovement.created_at < _next_month(start),
            BillingMovement.created_at <= until,
        )
    )
    return sum(units for at, units in rows.tuples() if counts_for_limit(history, at))


async def benefit_allowance(
    session: AsyncSession,
    account_id: uuid.UUID,
    month: date,
    *,
    now: datetime | None = None,
) -> Allowance:
    """The limit of ``month``, its use and what is left, as known at ``now``.

    A change after ``now`` is not known yet; the days of the month after
    ``now`` count with the setting in force at ``now``.
    """
    now = now or datetime.now(UTC)
    history = await settings_history(session, account_id, until=now)
    limit = month_limit(history, month)
    used = await _used(session, account_id, history, month, now)
    return Allowance(
        month=month_start(month).date(),
        benefit=setting_on(history, now + timedelta(microseconds=1)).benefit,
        limit_units=limit,
        used_units=used,
        remaining_units=max(0, limit - used),
    )


async def covered_units(
    session: AsyncSession,
    account: BillingAccount,
    action_kind: ActionKind,
    fact_units: int,
    *,
    now: datetime,
) -> int:
    """How many of ``fact_units`` the payer's benefit covers now (0 to fact)."""
    if (
        fact_units <= 0
        or account.kind != AccountKind.AUTHOR.value
        or action_kind not in COVERED_ACTIONS
    ):
        return 0
    allowance = await benefit_allowance(
        session, account.id, now.astimezone(UTC).date(), now=now
    )
    if allowance.benefit is Benefit.UNLIMITED:
        return fact_units
    if allowance.benefit is Benefit.MONTHLY_LIMIT:
        return min(fact_units, allowance.remaining_units)
    return 0
