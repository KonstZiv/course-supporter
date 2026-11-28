"""The promotion funnel (Б-4): enrolled → first submission → first purchase →
repeat purchase, with the money of the purchases.

Read-only. The base is the enrollment events (``enrollment_events``) whose
``created_at`` falls in an inclusive UTC calendar interval; deleting a student
or withdrawing a grant leaves them. For each event:

- ``first_submission`` — the student has at least one homework submission in
  that course at or after the event;
- ``first_purchase`` — the student's own billing account in that tenant has at
  least one paid purchase (``billing_batches.source = 'purchase'``, not marked
  as a test one in ``billing_test_batches``) at or after the event;
- ``repeat_purchase`` — two or more of them.

The figures of a group count events, so a student enrolled in two courses of
one tenant counts in each course. The money is the tenant's, not a course's:
``purchased_money`` is the sum of those purchases (``amount_paid_minor``, tax
included, per currency) of the group's distinct students, each from their
earliest event in the group in the period — and only by ``path`` and
``tenant``, where one student's money cannot fall into two rows; by ``course``
and ``source`` it is ``None``. Refunds are not subtracted. Purchases are taken
up to the moment of the run, not only within the period.

Gifts (``admin_credit``) are not purchases. Service students
(``students.is_test_student``) are left out unless ``include_test_students``;
the report counts the students left out. A deleted student's account is
anonymized (its ``tenant_id`` and ``student_id`` erased), so the purchases of a
deleted student are no longer found: their event still counts as enrolled.

One function, :func:`build_funnel`, for the administrator's command today and
for a panel route later.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, get_args

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.costs import utc_bounds
from course_supporter.billing.kinds import AccountKind, BatchSource
from course_supporter.billing.promotion import PromotionPath
from course_supporter.storage.orm import (
    BillingAccount,
    BillingBatch,
    BillingTestBatch,
    CourseNode,
    EnrollmentEvent,
    HomeworkSubmission,
    Student,
    Tenant,
)

FunnelScope = Literal["path", "tenant", "course", "source"]
FUNNEL_SCOPES: tuple[FunnelScope, ...] = get_args(FunnelScope)
MONEY_SCOPES: frozenset[FunnelScope] = frozenset({"path", "tenant"})
"""The scopes that carry ``purchased_money`` (module docstring)."""

Money = tuple[tuple[str, int], ...]
"""``((currency, minor units), ...)`` sorted by currency."""

_PATH_ORDER = {path.value: i for i, path in enumerate(PromotionPath)}


@dataclass(frozen=True, slots=True)
class FunnelGroup:
    """The key of a row; only the fields of the report's scope are set."""

    path: str | None = None
    tenant_id: uuid.UUID | None = None
    tenant_name: str | None = None
    course_id: uuid.UUID | None = None
    course_title: str | None = None
    source: str | None = None
    source_tag: str | None = None


@dataclass(frozen=True, slots=True)
class FunnelRow:
    """The funnel of one group (or the total, with an empty group)."""

    group: FunnelGroup
    enrolled: int
    first_submission: int
    first_purchase: int
    repeat_purchase: int
    purchased_money: Money | None
    """``None`` where the scope does not carry money (``course``, ``source``)."""


@dataclass(frozen=True, slots=True)
class FunnelReport:
    from_date: date
    to_date: date
    by: FunnelScope
    include_test_students: bool
    rows: tuple[FunnelRow, ...]
    total: FunnelRow
    excluded_test_students: int
    """Distinct service students whose events were left out (0 when included)."""


@dataclass(frozen=True, slots=True)
class _Event:
    tenant_id: uuid.UUID
    tenant_name: str
    path: str | None
    course_id: uuid.UUID
    course_title: str | None
    student_id: uuid.UUID
    source: str
    source_tag: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Purchase:
    created_at: datetime
    currency: str
    amount_minor: int


def _group_of(event: _Event, by: FunnelScope) -> FunnelGroup:
    if by == "path":
        return FunnelGroup(path=event.path)
    if by == "tenant":
        return FunnelGroup(tenant_id=event.tenant_id, tenant_name=event.tenant_name)
    if by == "course":
        return FunnelGroup(
            tenant_id=event.tenant_id,
            tenant_name=event.tenant_name,
            course_id=event.course_id,
            course_title=event.course_title,
        )
    return FunnelGroup(source=event.source, source_tag=event.source_tag)


def _sort_key(group: FunnelGroup) -> tuple[object, ...]:
    """Paths in their declared order, unset last; then names, then ids."""
    path_rank = _PATH_ORDER.get(group.path or "", len(_PATH_ORDER))
    return (
        path_rank,
        group.tenant_name or "",
        str(group.tenant_id or ""),
        group.course_title or "",
        str(group.course_id or ""),
        group.source or "",
        group.source_tag or "",
    )


async def _events(
    session: AsyncSession,
    *,
    start: datetime,
    end_exclusive: datetime,
) -> list[tuple[_Event, bool]]:
    """The period's events with the student's service mark."""
    rows = (
        await session.execute(
            select(
                EnrollmentEvent,
                Tenant.name,
                Tenant.promotion_path,
                CourseNode.title,
                Student.is_test_student,
            )
            .join(Tenant, Tenant.id == EnrollmentEvent.tenant_id)
            .join(CourseNode, CourseNode.id == EnrollmentEvent.course_node_id)
            .join(Student, Student.id == EnrollmentEvent.student_id)
            .where(
                EnrollmentEvent.created_at >= start,
                EnrollmentEvent.created_at < end_exclusive,
            )
            .order_by(EnrollmentEvent.created_at, EnrollmentEvent.id)
        )
    ).all()
    return [
        (
            _Event(
                tenant_id=ev.tenant_id,
                tenant_name=tenant_name,
                path=path,
                course_id=ev.course_node_id,
                course_title=title,
                student_id=ev.student_id,
                source=ev.source,
                source_tag=ev.source_tag,
                created_at=ev.created_at,
            ),
            bool(is_test),
        )
        for ev, tenant_name, path, title, is_test in rows
    ]


async def _last_submissions(
    session: AsyncSession, student_ids: Iterable[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], datetime]:
    """The latest submission per (student, course): a submission at or after
    an event exists iff the latest one is."""
    rows = (
        await session.execute(
            select(
                HomeworkSubmission.student_id,
                HomeworkSubmission.course_node_id,
                func.max(HomeworkSubmission.created_at),
            )
            .where(HomeworkSubmission.student_id.in_(list(student_ids)))
            .group_by(HomeworkSubmission.student_id, HomeworkSubmission.course_node_id)
        )
    ).all()
    return {(sid, cid): last for sid, cid, last in rows}


async def _paid_purchases(
    session: AsyncSession, student_ids: Iterable[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], list[_Purchase]]:
    """Paid, non-test purchases on the students' own accounts, per (tenant,
    student)."""
    test_marked = select(BillingTestBatch.batch_id)
    rows = (
        await session.execute(
            select(
                BillingAccount.tenant_id,
                BillingAccount.student_id,
                BillingBatch.created_at,
                BillingBatch.currency,
                BillingBatch.amount_paid_minor,
            )
            .join(BillingAccount, BillingAccount.id == BillingBatch.account_id)
            .where(
                BillingAccount.kind == AccountKind.STUDENT.value,
                BillingAccount.student_id.in_(list(student_ids)),
                BillingBatch.source == BatchSource.PURCHASE.value,
                BillingBatch.amount_paid_minor.is_not(None),
                BillingBatch.id.not_in(test_marked),
            )
        )
    ).all()
    found: dict[tuple[uuid.UUID, uuid.UUID], list[_Purchase]] = defaultdict(list)
    for tenant_id, student_id, created_at, currency, minor in rows:
        found[(tenant_id, student_id)].append(
            _Purchase(created_at=created_at, currency=currency, amount_minor=minor)
        )
    return found


def _money(amounts: dict[str, int]) -> Money:
    return tuple(sorted(amounts.items()))


def _row(
    group: FunnelGroup,
    events: list[_Event],
    *,
    with_money: bool,
    submissions: dict[tuple[uuid.UUID, uuid.UUID], datetime],
    purchases: dict[tuple[uuid.UUID, uuid.UUID], list[_Purchase]],
) -> FunnelRow:
    submitted = paid_once = paid_again = 0
    for ev in events:
        last = submissions.get((ev.student_id, ev.course_id))
        if last is not None and last >= ev.created_at:
            submitted += 1
        paid = sum(
            1
            for p in purchases.get((ev.tenant_id, ev.student_id), ())
            if p.created_at >= ev.created_at
        )
        paid_once += paid >= 1
        paid_again += paid >= 2
    money: Money | None = None
    if with_money:
        earliest: dict[tuple[uuid.UUID, uuid.UUID], datetime] = {}
        for ev in events:
            key = (ev.tenant_id, ev.student_id)
            if key not in earliest or ev.created_at < earliest[key]:
                earliest[key] = ev.created_at
        amounts: dict[str, int] = defaultdict(int)
        for key, since in earliest.items():
            for p in purchases.get(key, ()):
                if p.created_at >= since:
                    amounts[p.currency] += p.amount_minor
        money = _money(amounts)
    return FunnelRow(
        group=group,
        enrolled=len(events),
        first_submission=submitted,
        first_purchase=paid_once,
        repeat_purchase=paid_again,
        purchased_money=money,
    )


def _sum_money(rows: Iterable[FunnelRow]) -> Money:
    amounts: dict[str, int] = defaultdict(int)
    for row in rows:
        for currency, minor in row.purchased_money or ():
            amounts[currency] += minor
    return _money(amounts)


async def build_funnel(
    session: AsyncSession,
    *,
    from_date: date,
    to_date: date,
    by: FunnelScope,
    include_test_students: bool = False,
) -> FunnelReport:
    """The funnel of the events in ``[from_date, to_date]`` (UTC, inclusive),
    grouped by ``by`` (module docstring)."""
    start, end_exclusive = utc_bounds(from_date, to_date)
    found = await _events(session, start=start, end_exclusive=end_exclusive)
    excluded = (
        set()
        if include_test_students
        else {ev.student_id for ev, is_test in found if is_test}
    )
    events = [ev for ev, is_test in found if include_test_students or not is_test]
    student_ids = {ev.student_id for ev in events}
    submissions = await _last_submissions(session, student_ids) if events else {}
    purchases = await _paid_purchases(session, student_ids) if events else {}

    grouped: dict[FunnelGroup, list[_Event]] = defaultdict(list)
    for ev in events:
        grouped[_group_of(ev, by)].append(ev)
    with_money = by in MONEY_SCOPES
    rows = tuple(
        _row(
            group,
            grouped[group],
            with_money=with_money,
            submissions=submissions,
            purchases=purchases,
        )
        for group in sorted(grouped, key=_sort_key)
    )
    total = FunnelRow(
        group=FunnelGroup(),
        enrolled=sum(r.enrolled for r in rows),
        first_submission=sum(r.first_submission for r in rows),
        first_purchase=sum(r.first_purchase for r in rows),
        repeat_purchase=sum(r.repeat_purchase for r in rows),
        # path and tenant groups split the tenants, so their sums add up.
        purchased_money=_sum_money(rows) if with_money else None,
    )
    return FunnelReport(
        from_date=from_date,
        to_date=to_date,
        by=by,
        include_test_students=include_test_students,
        rows=rows,
        total=total,
        excluded_test_students=len(excluded),
    )
