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

from course_supporter.billing.errors import BillingInvalidError
from course_supporter.cost_kinds import CostKind
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Tenant,
)

ZERO_USD = Decimal("0")

CostReportScope = Literal["author", "student", "both"]

PREPARATION_STAGES: frozenset[str] = frozenset(
    {"criteria_decomposition", "key_explanation"}
)

PROCESSING_JOB_TYPES: tuple[str, ...] = (
    JobType.DOCUMENT_PROCESSING.value,
    JobType.NODE_SUMMARY_REGENERATION.value,
    JobType.DOCUMENT_PREPARATION.value,
    JobType.BASE_NORMALIZE.value,
)

_PAID_CALL = ExternalServiceCall.cost_kind.is_not(None)

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


@dataclass(frozen=True, slots=True)
class AuthorCostTotals:
    """Totals row across all authors in the report."""

    spend: SpendSummary
    processing: ActionSpend
    task_preparation: ActionSpend
    review: ActionSpend
    other: ActionSpend


@dataclass(frozen=True, slots=True)
class StudentCostRow:
    """Aggregated external service spend for one student (ID only, no PII)."""

    tenant_id: uuid.UUID
    tenant_name: str | None
    student_id: uuid.UUID
    submissions: int
    spend: SpendSummary


@dataclass(frozen=True, slots=True)
class StudentCostTotals:
    """Totals row across all students in the report."""

    submissions: int
    spend: SpendSummary


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


def _utc_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
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

    author_rows: list[AuthorCostRow] = []
    total_spend = SpendSummary.zero()
    total_processing = ActionSpend.zero()
    total_prep = ActionSpend.zero()
    total_review = ActionSpend.zero()
    total_other = ActionSpend.zero()

    for (t_id, t_name), buckets in grouped.items():
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

        author_rows.append(
            AuthorCostRow(
                tenant_id=t_id,
                tenant_name=t_name,
                spend=row_spend,
                processing=processing,
                task_preparation=task_preparation,
                review=review,
                other=other,
            )
        )
        total_spend = total_spend + row_spend
        total_processing = total_processing + processing
        total_prep = total_prep + task_preparation
        total_review = total_review + review
        total_other = total_other + other

    totals = AuthorCostTotals(
        spend=total_spend,
        processing=total_processing,
        task_preparation=total_prep,
        review=total_review,
        other=total_other,
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

    student_rows: list[StudentCostRow] = []
    total_submissions = 0
    total_spend = SpendSummary.zero()

    for row in rows:
        submissions = int(row.submissions or 0)
        spend = _spend_from_row(row)
        student_rows.append(
            StudentCostRow(
                tenant_id=row.tenant_id,
                tenant_name=row.tenant_name,
                student_id=row.student_id,
                submissions=submissions,
                spend=spend,
            )
        )
        total_submissions += submissions
        total_spend = total_spend + spend

    totals = StudentCostTotals(submissions=total_submissions, spend=total_spend)
    return tuple(student_rows), totals


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
        by: Which tables to populate (``'author'``, ``'student'``, or ``'both'``).
    """
    if from_date > to_date:
        raise BillingInvalidError(
            f"--from ({from_date.isoformat()}) must be <= --to ({to_date.isoformat()})"
        )

    start_dt, end_dt_exclusive = _utc_bounds(from_date, to_date)

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

    return CostReport(
        from_date=from_date,
        to_date=to_date,
        tenant_id=tenant_id,
        by=by,
        authors=authors,
        authors_total=authors_total,
        students=students,
        students_total=students_total,
    )
