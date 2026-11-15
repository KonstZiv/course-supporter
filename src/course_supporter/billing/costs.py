"""Read-only administrator cost report queries (BIL-COSTS).

Reads real external service costs (USD) from ``external_service_calls`` grouped
by author (tenant) and by student for an inclusive UTC calendar date range.
This module performs reads only; it never mutates database state or writes
billing audit entries.

Attribution rules (operator decisions 2026-11-06 / 2026-11-11):
- Only paid calls (``cost_kind IS NOT NULL``) are counted; non-call trace rows
  such as ``funds_port`` or ``review_metrics`` are ignored.
- Calls with ``action IN ('criteria_decomposition', 'key_explanation')`` or on a
  ``key_explanation`` job belong to author ``task_preparation`` even when executed
  inside a ``homework_processing`` job, and are excluded from per-student rows.
- Calls with ``cost_kind = 'unknown'`` have ``cost_usd IS NULL``; they are
  included in call counts and counted explicitly in ``unknown_calls``.

Units billed beside the dollars (BIL-E2, operator 2026-11-15, 5b item 3):
- billed — ``debit`` + ``difference_arisen`` of an action, in two columns by
  the account it is on: billed to the author (the tenant's author account) and
  billed to students (the tenant's student accounts); ``covered`` —
  ``benefit_covered`` + ``platform_covered``: borne by the benefit or the
  platform, billed to no one (:class:`BilledUnits`).
- An author row: the movements on the tenant's accounts; a student row: the
  movements of reviews for that student, whoever paid. The dollars are dated
  by the call, the units by the movement of the journal — an action that
  straddles the period's edge can show its dollars in one period and its
  units in the next (:data:`DATING_NOTE`), and a row may have units without
  calls.
- ``--by task`` (``by="task"``): author → course → homework task, dollars and
  units through the one link :func:`.links.link_actions`; a processing is its
  course's (no task), an action that leads to no course is a row without one.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, Row, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import AccountKind, ActionKind, MovementKind
from course_supporter.billing.links import (
    PREPARATION_STAGES,
    PROCESSING_JOB_TYPES,
    ActionRef,
    action_of_call,
    link_actions,
)
from course_supporter.cost_kinds import CostKind
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingMovement,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Tenant,
)

ZERO_USD = Decimal("0")

CostReportScope = Literal["author", "student", "both", "task"]


_PAID_CALL = ExternalServiceCall.cost_kind.is_not(None)

DATING_NOTE = (
    "Долари датовано викликом, юніти — рухом журналу: на межі періоду "
    "вони можуть розійтися."
)
"""The line of the report's head about the two datings."""

BILLED_KINDS: frozenset[str] = frozenset(
    {MovementKind.DEBIT.value, MovementKind.DIFFERENCE_ARISEN.value}
)
"""Movements of an action charged to its payer."""

COVERED_KINDS: frozenset[str] = frozenset(
    {MovementKind.BENEFIT_COVERED.value, MovementKind.PLATFORM_COVERED.value}
)
"""Movements of an action borne by the benefit or the platform."""

_JOB_OF_SUBMISSION = and_(
    Job.subject_type == JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
    Job.subject_id == HomeworkSubmission.id,
)


@dataclass(frozen=True, slots=True)
class SpendSummary:
    """Call counts and known USD totals broken down by ``cost_kind``."""

    calls: int
    exact_calls: int
    exact_usd: Decimal
    estimated_calls: int
    estimated_usd: Decimal
    unknown_calls: int

    @property
    def known_usd(self) -> Decimal:
        """Total USD from calls with ``exact`` or ``estimated`` cost."""
        return self.exact_usd + self.estimated_usd

    @classmethod
    def zero(cls) -> SpendSummary:
        """Return an all-zero spend summary."""
        return cls(
            calls=0,
            exact_calls=0,
            exact_usd=ZERO_USD,
            estimated_calls=0,
            estimated_usd=ZERO_USD,
            unknown_calls=0,
        )

    def __add__(self, other: SpendSummary) -> SpendSummary:
        return SpendSummary(
            calls=self.calls + other.calls,
            exact_calls=self.exact_calls + other.exact_calls,
            exact_usd=self.exact_usd + other.exact_usd,
            estimated_calls=self.estimated_calls + other.estimated_calls,
            estimated_usd=self.estimated_usd + other.estimated_usd,
            unknown_calls=self.unknown_calls + other.unknown_calls,
        )


@dataclass(frozen=True, slots=True)
class UnitsSummary:
    """Units of some actions: billed to their payers, and covered."""

    billed_units: int = 0
    covered_units: int = 0

    @classmethod
    def of(cls, kind: str, units: int) -> UnitsSummary:
        """One movement's units, by its kind (billed or covered)."""
        if kind in BILLED_KINDS:
            return cls(billed_units=units)
        if kind in COVERED_KINDS:
            return cls(covered_units=units)
        return cls()

    def __add__(self, other: UnitsSummary) -> UnitsSummary:
        return UnitsSummary(
            billed_units=self.billed_units + other.billed_units,
            covered_units=self.covered_units + other.covered_units,
        )


@dataclass(frozen=True, slots=True)
class BilledUnits:
    """Units of some actions in the cost report: billed to the author's
    account, billed to students' accounts, and covered (by the benefit or the
    platform — billed to no one)."""

    author_units: int = 0
    students_units: int = 0
    covered_units: int = 0

    @classmethod
    def of(cls, kind: str, units: int, *, payer_kind: str) -> BilledUnits:
        """One movement's units, by its kind and the kind of the account it is on."""
        if kind in COVERED_KINDS:
            return cls(covered_units=units)
        if kind not in BILLED_KINDS:
            return cls()
        if payer_kind == AccountKind.AUTHOR.value:
            return cls(author_units=units)
        return cls(students_units=units)

    def __add__(self, other: BilledUnits) -> BilledUnits:
        return BilledUnits(
            author_units=self.author_units + other.author_units,
            students_units=self.students_units + other.students_units,
            covered_units=self.covered_units + other.covered_units,
        )


@dataclass(frozen=True, slots=True)
class ActionSpend:
    """Spend totals for one author action category."""

    calls: int
    known_usd: Decimal
    unknown_calls: int

    @classmethod
    def zero(cls) -> ActionSpend:
        """Return an empty action spend bucket."""
        return cls(calls=0, known_usd=ZERO_USD, unknown_calls=0)

    def __add__(self, other: ActionSpend) -> ActionSpend:
        return ActionSpend(
            calls=self.calls + other.calls,
            known_usd=self.known_usd + other.known_usd,
            unknown_calls=self.unknown_calls + other.unknown_calls,
        )


@dataclass(frozen=True, slots=True)
class AuthorCostRow:
    """Aggregated external service spend for one author (tenant)."""

    tenant_id: uuid.UUID | None
    tenant_name: str | None
    spend: SpendSummary
    processing: ActionSpend
    task_preparation: ActionSpend
    review: ActionSpend
    other: ActionSpend
    units: BilledUnits = BilledUnits()


@dataclass(frozen=True, slots=True)
class AuthorCostTotals:
    """Totals row across all authors in the report."""

    spend: SpendSummary
    processing: ActionSpend
    task_preparation: ActionSpend
    review: ActionSpend
    other: ActionSpend
    units: BilledUnits = BilledUnits()


@dataclass(frozen=True, slots=True)
class StudentCostRow:
    """Aggregated external service spend for one student (ID only, no PII)."""

    tenant_id: uuid.UUID
    tenant_name: str | None
    student_id: uuid.UUID
    submissions: int
    spend: SpendSummary
    units: BilledUnits = BilledUnits()


@dataclass(frozen=True, slots=True)
class StudentCostTotals:
    """Totals row across all students in the report."""

    submissions: int
    spend: SpendSummary
    units: BilledUnits = BilledUnits()


@dataclass(frozen=True, slots=True)
class TaskCostRow:
    """One homework task of a course of an author: dollars and units.

    ``authored_document_id`` is ``None`` for the course's actions that are no
    task's (processing); ``course_node_id`` is ``None`` for actions that lead
    to no course.
    """

    tenant_id: uuid.UUID | None
    tenant_name: str | None
    course_node_id: uuid.UUID | None
    course_title: str | None
    authored_document_id: uuid.UUID | None
    task_title: str | None
    spend: SpendSummary
    units: BilledUnits


@dataclass(frozen=True, slots=True)
class TaskCostTotals:
    """Totals row across all tasks in the report."""

    spend: SpendSummary
    units: BilledUnits


@dataclass(frozen=True, slots=True)
class CostReport:
    """Complete administrator cost report for a UTC date interval."""

    from_date: date
    to_date: date
    tenant_id: uuid.UUID | None
    by: CostReportScope
    authors: tuple[AuthorCostRow, ...]
    authors_total: AuthorCostTotals
    students: tuple[StudentCostRow, ...]
    students_total: StudentCostTotals
    tasks: tuple[TaskCostRow, ...] = ()
    tasks_total: TaskCostTotals = TaskCostTotals(
        spend=SpendSummary.zero(), units=BilledUnits()
    )


def default_month_bounds(now: datetime | None = None) -> tuple[date, date]:
    """Return ``(first_day_of_utc_month, today_utc)``."""
    current = (now or datetime.now(UTC)).astimezone(UTC).date()
    return current.replace(day=1), current


def _to_decimal(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return ZERO_USD
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _spend_columns() -> tuple[ColumnElement[Any], ...]:
    return (
        func.count(ExternalServiceCall.id).label("calls"),
        func.count(ExternalServiceCall.id)
        .filter(ExternalServiceCall.cost_kind == CostKind.EXACT.value)
        .label("exact_calls"),
        func.sum(ExternalServiceCall.cost_usd)
        .filter(ExternalServiceCall.cost_kind == CostKind.EXACT.value)
        .label("exact_usd"),
        func.count(ExternalServiceCall.id)
        .filter(ExternalServiceCall.cost_kind == CostKind.ESTIMATED.value)
        .label("estimated_calls"),
        func.sum(ExternalServiceCall.cost_usd)
        .filter(ExternalServiceCall.cost_kind == CostKind.ESTIMATED.value)
        .label("estimated_usd"),
        func.count(ExternalServiceCall.id)
        .filter(ExternalServiceCall.cost_kind == CostKind.UNKNOWN.value)
        .label("unknown_calls"),
    )


def _spend_from_row(row: Row[Any]) -> SpendSummary:
    return SpendSummary(
        calls=int(row.calls or 0),
        exact_calls=int(row.exact_calls or 0),
        exact_usd=_to_decimal(row.exact_usd),
        estimated_calls=int(row.estimated_calls or 0),
        estimated_usd=_to_decimal(row.estimated_usd),
        unknown_calls=int(row.unknown_calls or 0),
    )


def _action_bucket_expr() -> ColumnElement[str]:
    is_task_prep = or_(
        ExternalServiceCall.action.in_(PREPARATION_STAGES),
        Job.job_type == JobType.KEY_EXPLANATION.value,
    )
    is_processing = Job.job_type.in_(PROCESSING_JOB_TYPES)
    is_review = or_(
        Job.job_type == JobType.HOMEWORK_PROCESSING.value,
        Job.subject_type == JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
    )
    return case(
        (is_task_prep, "task_preparation"),
        (is_processing, "processing"),
        (is_review, "review"),
        else_="other",
    )


def utc_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """``[from_date 00:00 UTC, to_date + 1 day 00:00 UTC)`` — inclusive dates."""
    start_dt = datetime.combine(from_date, time.min, tzinfo=UTC)
    end_dt_exclusive = datetime.combine(
        to_date + timedelta(days=1), time.min, tzinfo=UTC
    )
    return start_dt, end_dt_exclusive


async def _query_authors(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> tuple[tuple[AuthorCostRow, ...], AuthorCostTotals]:
    effective_tenant_id = func.coalesce(Job.tenant_id, HomeworkSubmission.tenant_id)
    bucket_col = _action_bucket_expr().label("bucket")

    stmt = (
        select(
            effective_tenant_id.label("tenant_id"),
            Tenant.name.label("tenant_name"),
            bucket_col,
            *_spend_columns(),
        )
        .select_from(ExternalServiceCall)
        .join(Job, Job.id == ExternalServiceCall.job_id)
        .outerjoin(HomeworkSubmission, _JOB_OF_SUBMISSION)
        .outerjoin(Tenant, Tenant.id == effective_tenant_id)
        .where(
            _PAID_CALL,
            ExternalServiceCall.created_at >= start_dt,
            ExternalServiceCall.created_at < end_dt_exclusive,
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(effective_tenant_id == tenant_id)
    stmt = stmt.group_by(effective_tenant_id, Tenant.name, bucket_col).order_by(
        func.coalesce(Tenant.name, "").asc(),
        effective_tenant_id.asc(),
    )

    rows = (await session.execute(stmt)).all()

    grouped: dict[tuple[uuid.UUID | None, str | None], dict[str, SpendSummary]] = (
        defaultdict(dict)
    )
    for row in rows:
        key = (row.tenant_id, row.tenant_name)
        bucket = str(row.bucket)
        spend = _spend_from_row(row)
        existing = grouped[key].get(bucket)
        grouped[key][bucket] = spend if existing is None else existing + spend

    units = await _units_by_tenant(
        session,
        start_dt=start_dt,
        end_dt_exclusive=end_dt_exclusive,
        tenant_id=tenant_id,
    )
    known = {key[0] for key in grouped}
    names = await _tenant_names(session, set(units) - known)
    for only_units in set(units) - known:
        grouped[(only_units, names.get(only_units))] = {}

    author_rows: list[AuthorCostRow] = []
    total_spend = SpendSummary.zero()
    total_processing = ActionSpend.zero()
    total_prep = ActionSpend.zero()
    total_review = ActionSpend.zero()
    total_other = ActionSpend.zero()
    total_units = BilledUnits()

    for (t_id, t_name), buckets in sorted(
        grouped.items(), key=lambda item: (item[0][1] or "", str(item[0][0]))
    ):
        row_spend = SpendSummary.zero()
        for spend in buckets.values():
            row_spend = row_spend + spend

        def _action_spend(
            name: str, b: dict[str, SpendSummary] = buckets
        ) -> ActionSpend:
            s = b.get(name)
            if s is None:
                return ActionSpend.zero()
            return ActionSpend(
                calls=s.calls, known_usd=s.known_usd, unknown_calls=s.unknown_calls
            )

        processing = _action_spend("processing")
        task_preparation = _action_spend("task_preparation")
        review = _action_spend("review")
        other = _action_spend("other")
        row_units = units.get(t_id, BilledUnits()) if t_id else BilledUnits()

        author_rows.append(
            AuthorCostRow(
                tenant_id=t_id,
                tenant_name=t_name,
                spend=row_spend,
                processing=processing,
                task_preparation=task_preparation,
                review=review,
                other=other,
                units=row_units,
            )
        )
        total_spend = total_spend + row_spend
        total_processing = total_processing + processing
        total_prep = total_prep + task_preparation
        total_review = total_review + review
        total_other = total_other + other
        total_units = total_units + row_units

    totals = AuthorCostTotals(
        spend=total_spend,
        processing=total_processing,
        task_preparation=total_prep,
        review=total_review,
        other=total_other,
        units=total_units,
    )
    return tuple(author_rows), totals


async def _query_students(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> tuple[tuple[StudentCostRow, ...], StudentCostTotals]:
    effective_tenant_id = func.coalesce(Job.tenant_id, HomeworkSubmission.tenant_id)

    stmt = (
        select(
            effective_tenant_id.label("tenant_id"),
            Tenant.name.label("tenant_name"),
            HomeworkSubmission.student_id.label("student_id"),
            func.count(func.distinct(HomeworkSubmission.id)).label("submissions"),
            *_spend_columns(),
        )
        .select_from(ExternalServiceCall)
        .join(Job, Job.id == ExternalServiceCall.job_id)
        .join(HomeworkSubmission, _JOB_OF_SUBMISSION)
        .outerjoin(Tenant, Tenant.id == effective_tenant_id)
        .where(
            _PAID_CALL,
            ExternalServiceCall.created_at >= start_dt,
            ExternalServiceCall.created_at < end_dt_exclusive,
            ExternalServiceCall.action.notin_(PREPARATION_STAGES),
            Job.job_type.is_distinct_from(JobType.KEY_EXPLANATION.value),
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(effective_tenant_id == tenant_id)
    stmt = stmt.group_by(
        effective_tenant_id,
        Tenant.name,
        HomeworkSubmission.student_id,
    ).order_by(
        func.coalesce(Tenant.name, "").asc(),
        effective_tenant_id.asc(),
        HomeworkSubmission.student_id.asc(),
    )

    rows = (await session.execute(stmt)).all()
    units = await _units_by_student(
        session,
        start_dt=start_dt,
        end_dt_exclusive=end_dt_exclusive,
        tenant_id=tenant_id,
    )

    found: dict[tuple[uuid.UUID, uuid.UUID], StudentCostRow] = {}
    for row in rows:
        key = (row.tenant_id, row.student_id)
        found[key] = StudentCostRow(
            tenant_id=row.tenant_id,
            tenant_name=row.tenant_name,
            student_id=row.student_id,
            submissions=int(row.submissions or 0),
            spend=_spend_from_row(row),
            units=units.get(key, BilledUnits()),
        )
    names = await _tenant_names(session, {t for t, _ in set(units) - set(found)})
    for t_id, s_id in set(units) - set(found):
        found[(t_id, s_id)] = StudentCostRow(
            tenant_id=t_id,
            tenant_name=names.get(t_id),
            student_id=s_id,
            submissions=0,
            spend=SpendSummary.zero(),
            units=units[(t_id, s_id)],
        )
    student_rows = sorted(
        found.values(),
        key=lambda r: (r.tenant_name or "", str(r.tenant_id), str(r.student_id)),
    )

    total_submissions = 0
    total_spend = SpendSummary.zero()
    total_units = BilledUnits()
    for student_row in student_rows:
        total_submissions += student_row.submissions
        total_spend = total_spend + student_row.spend
        total_units = total_units + student_row.units

    totals = StudentCostTotals(
        submissions=total_submissions, spend=total_spend, units=total_units
    )
    return tuple(student_rows), totals


# ── Units billed, from the journal (BIL-E2) ───────────────────────────


def _units_of_actions() -> tuple[ColumnElement[bool], ...]:
    """Movements of an action that are billed or covered."""
    return (
        BillingMovement.kind.in_(BILLED_KINDS | COVERED_KINDS),
        BillingMovement.action_key.is_not(None),
    )


async def _tenant_names(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Tenant.id, Tenant.name).where(Tenant.id.in_(ids))
    )
    return dict(rows.tuples().all())


async def _units_by_tenant(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> dict[uuid.UUID, BilledUnits]:
    """Billed and covered units on each tenant's accounts (author's, students')."""
    stmt = (
        select(
            BillingAccount.tenant_id,
            BillingAccount.kind,
            BillingMovement.kind,
            func.sum(BillingMovement.units),
        )
        .join(BillingAccount, BillingAccount.id == BillingMovement.account_id)
        .where(
            *_units_of_actions(),
            BillingAccount.tenant_id.is_not(None),
            BillingMovement.created_at >= start_dt,
            BillingMovement.created_at < end_dt_exclusive,
        )
        .group_by(BillingAccount.tenant_id, BillingAccount.kind, BillingMovement.kind)
    )
    if tenant_id is not None:
        stmt = stmt.where(BillingAccount.tenant_id == tenant_id)
    found: dict[uuid.UUID, BilledUnits] = defaultdict(BilledUnits)
    for t_id, payer_kind, kind, units in (await session.execute(stmt)).tuples():
        if t_id is not None:
            found[t_id] = found[t_id] + BilledUnits.of(
                kind, int(units), payer_kind=payer_kind
            )
    return dict(found)


async def _units_by_student(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> dict[tuple[uuid.UUID, uuid.UUID], BilledUnits]:
    """Units of the reviews for each student: billed to the author (under a
    payment rule) or to the student, and covered."""
    beneficiary = aliased(BillingAccount)
    payer = aliased(BillingAccount)
    stmt = (
        select(
            beneficiary.tenant_id,
            beneficiary.student_id,
            payer.kind,
            BillingMovement.kind,
            func.sum(BillingMovement.units),
        )
        .join(beneficiary, beneficiary.id == BillingMovement.beneficiary_account_id)
        .join(payer, payer.id == BillingMovement.account_id)
        .where(
            *_units_of_actions(),
            BillingMovement.action_kind == ActionKind.REVIEW.value,
            beneficiary.kind == AccountKind.STUDENT.value,
            beneficiary.tenant_id.is_not(None),
            beneficiary.student_id.is_not(None),
            BillingMovement.created_at >= start_dt,
            BillingMovement.created_at < end_dt_exclusive,
        )
        .group_by(
            beneficiary.tenant_id,
            beneficiary.student_id,
            payer.kind,
            BillingMovement.kind,
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(beneficiary.tenant_id == tenant_id)
    found: dict[tuple[uuid.UUID, uuid.UUID], BilledUnits] = defaultdict(BilledUnits)
    for t_id, s_id, payer_kind, kind, units in (await session.execute(stmt)).tuples():
        if t_id is not None and s_id is not None:
            found[(t_id, s_id)] = found[(t_id, s_id)] + BilledUnits.of(
                kind, int(units), payer_kind=payer_kind
            )
    return dict(found)


# ── By task: author → course → homework task (BIL-E2) ─────────────────

TaskKey = tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]
"""(tenant, course, task) of a row of the report by task."""


def _task_key(tenant: uuid.UUID | None, ref: ActionRef | None, link: Any) -> TaskKey:
    """The row an action goes to: a processing is its course's, not a task's."""
    if ref is None or link.course_node_id is None:
        return (tenant, None, None)
    task = None if ref.kind is ActionKind.PROCESSING else link.authored_document_id
    return (tenant, link.course_node_id, task)


async def _query_tasks(
    session: AsyncSession,
    *,
    start_dt: datetime,
    end_dt_exclusive: datetime,
    tenant_id: uuid.UUID | None,
) -> tuple[tuple[TaskCostRow, ...], TaskCostTotals]:
    effective_tenant_id = func.coalesce(Job.tenant_id, HomeworkSubmission.tenant_id)
    calls = (
        select(
            effective_tenant_id.label("tenant_id"),
            Job.id.label("job_id"),
            Job.job_type,
            Job.subject_id,
            *_spend_columns(),
        )
        .select_from(ExternalServiceCall)
        .join(Job, Job.id == ExternalServiceCall.job_id)
        .outerjoin(HomeworkSubmission, _JOB_OF_SUBMISSION)
        .where(
            _PAID_CALL,
            ExternalServiceCall.created_at >= start_dt,
            ExternalServiceCall.created_at < end_dt_exclusive,
        )
        .group_by(effective_tenant_id, Job.id, Job.job_type, Job.subject_id)
    )
    if tenant_id is not None:
        calls = calls.where(effective_tenant_id == tenant_id)
    spent = [
        (
            row.tenant_id,
            action_of_call(
                job_id=row.job_id, job_type=row.job_type, subject_id=row.subject_id
            ),
            _spend_from_row(row),
        )
        for row in (await session.execute(calls)).all()
    ]

    moves = (
        select(
            BillingAccount.tenant_id,
            BillingAccount.kind,
            BillingMovement.action_kind,
            BillingMovement.action_key,
            BillingMovement.kind,
            func.sum(BillingMovement.units),
        )
        .join(BillingAccount, BillingAccount.id == BillingMovement.account_id)
        .where(
            *_units_of_actions(),
            BillingMovement.created_at >= start_dt,
            BillingMovement.created_at < end_dt_exclusive,
        )
        .group_by(
            BillingAccount.tenant_id,
            BillingAccount.kind,
            BillingMovement.action_kind,
            BillingMovement.action_key,
            BillingMovement.kind,
        )
    )
    if tenant_id is not None:
        moves = moves.where(BillingAccount.tenant_id == tenant_id)
    moved = [
        (
            t_id,
            ActionRef(ActionKind(action_kind), key),
            BilledUnits.of(kind, int(n), payer_kind=payer_kind),
        )
        for t_id, payer_kind, action_kind, key, kind, n in (
            await session.execute(moves)
        ).tuples()
        if action_kind is not None and key is not None
    ]

    links = await link_actions(
        session,
        [ref for _, ref, _ in spent if ref is not None] + [ref for _, ref, _ in moved],
    )
    spends: dict[TaskKey, SpendSummary] = defaultdict(SpendSummary.zero)
    units: dict[TaskKey, BilledUnits] = defaultdict(BilledUnits)
    for t_id, call_ref, spend in spent:
        key = _task_key(t_id, call_ref, links.get(call_ref) if call_ref else None)
        spends[key] = spends[key] + spend
    for t_id, ref, summary in moved:
        key = _task_key(t_id, ref, links[ref])
        units[key] = units[key] + summary

    keys = set(spends) | set(units)
    tenants = await _tenant_names(session, {k[0] for k in keys if k[0] is not None})
    courses = await course_titles(session, {k[1] for k in keys if k[1] is not None})
    tasks = await task_titles(session, {k[2] for k in keys if k[2] is not None})
    task_rows = sorted(
        (
            TaskCostRow(
                tenant_id=t_id,
                tenant_name=tenants.get(t_id) if t_id else None,
                course_node_id=c_id,
                course_title=courses.get(c_id) if c_id else None,
                authored_document_id=d_id,
                task_title=tasks.get(d_id) if d_id else None,
                spend=spends.get((t_id, c_id, d_id), SpendSummary.zero()),
                units=units.get((t_id, c_id, d_id), BilledUnits()),
            )
            for t_id, c_id, d_id in keys
        ),
        key=lambda r: (
            r.tenant_name or "",
            str(r.tenant_id),
            r.course_node_id is None,
            r.course_title or "",
            str(r.course_node_id),
            r.authored_document_id is None,
            r.task_title or "",
            str(r.authored_document_id),
        ),
    )
    total_spend = SpendSummary.zero()
    total_units = BilledUnits()
    for task_row in task_rows:
        total_spend = total_spend + task_row.spend
        total_units = total_units + task_row.units
    return tuple(task_rows), TaskCostTotals(spend=total_spend, units=total_units)


async def course_titles(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Each course node's title."""
    if not ids:
        return {}
    rows = await session.execute(
        select(CourseNode.id, CourseNode.title).where(CourseNode.id.in_(ids))
    )
    return dict(rows.tuples().all())


async def task_titles(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """Each document's title; one without a title — its file name."""
    if not ids:
        return {}
    rows = await session.execute(
        select(
            AuthoredDocument.id, AuthoredDocument.title, AuthoredDocument.filename
        ).where(AuthoredDocument.id.in_(ids))
    )
    return {doc_id: title or filename for doc_id, title, filename in rows.tuples()}


async def build_cost_report(
    session: AsyncSession,
    *,
    from_date: date,
    to_date: date,
    tenant_id: uuid.UUID | None = None,
    by: CostReportScope = "both",
) -> CostReport:
    """Build a read-only external service cost report for ``[from_date, to_date]``.

    Args:
        session: Active async SQLAlchemy session. Callers running on PostgreSQL
            should issue ``SET TRANSACTION READ ONLY`` before any query in the
            transaction.
        from_date: Inclusive start UTC date.
        to_date: Inclusive end UTC date.
        tenant_id: Optional tenant filter.
        by: Which tables to populate (``'author'``, ``'student'``, ``'both'``,
            or ``'task'`` — author → course → homework task).
    """
    if from_date > to_date:
        raise BillingInvalidError(
            f"--from ({from_date.isoformat()}) must be <= --to ({to_date.isoformat()})"
        )

    start_dt, end_dt_exclusive = utc_bounds(from_date, to_date)

    authors: tuple[AuthorCostRow, ...] = ()
    authors_total = AuthorCostTotals(
        spend=SpendSummary.zero(),
        processing=ActionSpend.zero(),
        task_preparation=ActionSpend.zero(),
        review=ActionSpend.zero(),
        other=ActionSpend.zero(),
    )
    if by in ("author", "both"):
        authors, authors_total = await _query_authors(
            session,
            start_dt=start_dt,
            end_dt_exclusive=end_dt_exclusive,
            tenant_id=tenant_id,
        )

    students: tuple[StudentCostRow, ...] = ()
    students_total = StudentCostTotals(submissions=0, spend=SpendSummary.zero())
    if by in ("student", "both"):
        students, students_total = await _query_students(
            session,
            start_dt=start_dt,
            end_dt_exclusive=end_dt_exclusive,
            tenant_id=tenant_id,
        )

    tasks: tuple[TaskCostRow, ...] = ()
    tasks_total = TaskCostTotals(spend=SpendSummary.zero(), units=BilledUnits())
    if by == "task":
        tasks, tasks_total = await _query_tasks(
            session,
            start_dt=start_dt,
            end_dt_exclusive=end_dt_exclusive,
            tenant_id=tenant_id,
        )

    return CostReport(
        from_date=from_date,
        to_date=to_date,
        tenant_id=tenant_id,
        by=by,
        authors=authors,
        authors_total=authors_total,
        students=students,
        students_total=students_total,
        tasks=tasks,
        tasks_total=tasks_total,
    )
