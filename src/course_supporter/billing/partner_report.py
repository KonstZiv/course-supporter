"""The partner's report: a school's month of reviews in money (Б-3).

Operator 2026-11-27.

Purpose:
    The product owner negotiates with a school (a tenant): the school wants to
    see its students' money, the platform's costs, and what it could earn. The
    report is INTERNAL — the administrator's, ``scripts/billing_admin.py
    partner-report``; what of it is shown to the school is the product owner's
    decision. :func:`partner_report` is the one function a panel's route will
    call later.

What is counted (one month, UTC, both edges inclusive, as in :mod:`.spending`):

    * ``reviews`` — reviews the school's students paid from their own accounts
      (:mod:`.review_money`: a ``debit`` on a student's own account; a review
      the author pays for under a rule is not here). Service students
      (``students.is_test_student``) are left out; their reviews are counted
      in :attr:`PartnerReport.excluded_test_reviews`.
    * ``gross_minor`` — the students' money spent on those reviews, tax
      included: a debit from a paid batch is ``units * amount_paid_minor /
      batch.units``. The base of the share is money SPENT on reviews: units
      bought and not used are not in it. Units of batches without money are
      ``gift_units``.
    * ``tax_minor`` — each batch's tax in proportion to the units spent from
      it: ``units / batch.units * tax_minor`` of the provider's figures
      (``billing_batch_provider_figures``, :mod:`.provider_figures`). A batch
      without the provider's tax (not synced yet: ``creem-sync``) adds none
      and is counted in :attr:`MoneyLine.without_provider_data`; no tax is
      guessed.
    * ``fee_minor`` — the same of each batch's fee (Б-5, product owner
      2026-11-27): the provider's, where its API or event gave it
      (``fee_minor`` of its figures) — it has precedence; else the provider's
      tariff (``provider_fees`` of ``config/billing_packages.yaml``,
      :meth:`~.payments.catalog.FeeTariff.fee_minor`: ``round_half_up(
      amount_paid_minor x percent / 100) + fixed_minor`` of the batch's
      currency, once per batch — Creem's API gives no fee), named in
      :attr:`MoneyLine.fee_by_tariff` (:func:`tariff_note`: «комісія Creem —
      розрахунково за тарифом»). A batch with neither — the tariff has no
      fixed part in its currency, or the provider no tariff — adds none and
      names its provider in :attr:`MoneyLine.fee_unknown` (:func:`fee_note`:
      «комісія Creem: тариф для EUR не задано»); None when no batch of the
      line has a fee.
    * ``net_minor`` = gross - tax - fee (the fee where known) — so before the
      fee of a currency without a tariff and before the tax of batches
      without the provider's data: the line's marks say which.
    * ``model_cost_usd`` — what the models cost for these same reviews, in
      dollars, as the cost report counts a review (:mod:`.costs`: paid calls
      of the review's jobs, the preparation stages left out), whenever the
      calls were made; calls of unknown cost apart (``unknown_cost_calls``).
    * ``partner_share_minor`` = the school's rate * net
      (``billing_author_settings.partner_share_bps``); no rate — None
      (:data:`NO_RATE`).
    * refunds — money given back to the school's students in the month
      (``billing_refunds`` decided in it: a refund approved, a dispute lost),
      a line apart, subtracted from nothing above.

By course → task, and the totals; each currency apart. A review that leads to
no course of the school is in :attr:`PartnerReport.unlinked` (in the totals).
Rounding: per line and currency the money, tax and fee are summed exactly and
rounded once, half up; ``net_minor`` is gross - tax - fee of those whole
numbers, and the share is rounded once from it. The totals are rounded on
their own, not summed from rounded rows.

Read-only; every query carries the tenant (``impl-rules#9``).
"""

from __future__ import annotations

import calendar
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select

from course_supporter.billing.admin import get_author_settings
from course_supporter.billing.costs import ZERO_USD, task_titles, utc_bounds
from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    RefundState,
)
from course_supporter.billing.links import (
    PREPARATION_STAGES,
    ActionRef,
    link_actions,
)
from course_supporter.billing.payments.catalog import FeeTariff, get_catalog
from course_supporter.billing.review_money import (
    ReviewDebit,
    excluded_test_reviews,
    review_debits,
)
from course_supporter.cost_kinds import CostKind
from course_supporter.jobs.job_type import JobType
from course_supporter.storage.orm import (
    BillingAccount,
    BillingBatch,
    BillingBatchProviderFigures,
    BillingRefund,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

BPS: Final = 10_000
"""Hundredths of a per cent in a whole."""

NO_RATE: Final = "ставку не задано"
"""What the report says instead of a share when the school has no rate."""

_PROVIDER_NAMES: Final = {"creem": "Creem"}


def provider_label(provider: str) -> str:
    """How a person reads the provider's name."""
    return _PROVIDER_NAMES.get(provider, provider)


def tariff_note(provider: str) -> str:
    """The provider's fee in the net is counted by its tariff, not given."""
    return f"комісія {provider_label(provider)} — розрахунково за тарифом"


def fee_note(provider: str, currency: str) -> str:
    """Why a provider's fee is not in the net: no tariff in the currency."""
    return f"комісія {provider_label(provider)}: тариф для {currency} не задано"


def no_data_note(provider: str, batches: int) -> str:
    """Batches whose tax the provider has not told yet."""
    return f"без даних {provider_label(provider)}: {batches} парт."


def month_bounds(month: date) -> tuple[date, date]:
    """The first and the last day of the month of ``month``."""
    last = calendar.monthrange(month.year, month.month)[1]
    return month.replace(day=1), month.replace(day=last)


def _half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


# ── The figures ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MoneyLine:
    """One currency of a row (module docstring)."""

    currency: str
    gross_minor: int
    tax_minor: int
    fee_minor: int | None
    """None — no batch of the line has a fee (given, or by a tariff)."""
    net_minor: int
    partner_share_minor: int | None
    """None — the school has no rate (:data:`NO_RATE`)."""
    without_provider_data: tuple[tuple[str, int], ...]
    """``(provider, batches)`` whose tax the provider has not told yet."""
    fee_unknown: tuple[str, ...]
    """Providers of batches in the line whose fee is not known: not given,
    and no tariff in the line's currency (:func:`fee_note`)."""
    fee_by_tariff: tuple[str, ...]
    """Providers whose fee in the line is counted by their tariff
    (:func:`tariff_note`)."""


@dataclass(frozen=True, slots=True)
class PartnerFigures:
    """The reviews of a row and their money, per currency."""

    reviews: int
    gift_units: int
    model_cost_usd: Decimal
    unknown_cost_calls: int
    money: tuple[MoneyLine, ...]


@dataclass(frozen=True, slots=True)
class PartnerTask:
    """A homework task of a course."""

    authored_document_id: uuid.UUID
    title: str | None
    figures: PartnerFigures


@dataclass(frozen=True, slots=True)
class PartnerCourse:
    """A course of the school, and its tasks."""

    course_node_id: uuid.UUID
    title: str | None
    figures: PartnerFigures
    tasks: tuple[PartnerTask, ...]


@dataclass(frozen=True, slots=True)
class RefundLine:
    """Money given back to the school's students in the month, of one kind."""

    kind: str
    """``refund`` (approved) or ``dispute`` (lost)."""
    currency: str
    count: int
    units: int
    amount_minor: int


@dataclass(frozen=True, slots=True)
class PartnerReport:
    """The partner's report of a school for a month (module docstring)."""

    tenant_id: uuid.UUID
    tenant_name: str
    from_date: date
    to_date: date
    share_bps: int | None
    courses: tuple[PartnerCourse, ...]
    unlinked: PartnerFigures
    totals: PartnerFigures
    refunds: tuple[RefundLine, ...]
    excluded_test_reviews: int


# ── Exact sums, kept until rounding ────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Batch:
    provider: str
    units: int
    tax_minor: int | None
    fee_minor: int | None
    """The provider's, or by its tariff (``fee_by_tariff``); None — neither."""
    fee_by_tariff: bool = False


@dataclass(slots=True)
class _Money:
    """One currency of a row: exact sums, and what is not known."""

    gross: Fraction = Fraction(0)
    tax: Fraction = Fraction(0)
    fee: Fraction = Fraction(0)
    fee_known: bool = False
    fee_unknown: set[str] = field(default_factory=set)
    fee_by_tariff: set[str] = field(default_factory=set)
    no_data: dict[str, set[uuid.UUID]] = field(default_factory=lambda: defaultdict(set))

    def add(self, debit: ReviewDebit, batch: _Batch) -> None:
        """A paid debit: its money, and its batch's tax and fee for its units."""
        self.gross += debit.money
        part = Fraction(debit.units, batch.units)
        if batch.tax_minor is None:
            self.no_data[batch.provider].add(debit.batch_id)
        else:
            self.tax += part * batch.tax_minor
        if batch.fee_minor is None:
            self.fee_unknown.add(batch.provider)
        else:
            self.fee += part * batch.fee_minor
            self.fee_known = True
            if batch.fee_by_tariff:
                self.fee_by_tariff.add(batch.provider)

    def line(self, currency: str, share_bps: int | None) -> MoneyLine:
        gross, tax = _half_up(self.gross), _half_up(self.tax)
        fee = _half_up(self.fee) if self.fee_known else None
        net = gross - tax - (fee or 0)
        return MoneyLine(
            currency=currency,
            gross_minor=gross,
            tax_minor=tax,
            fee_minor=fee,
            net_minor=net,
            partner_share_minor=(
                None if share_bps is None else _half_up(Fraction(net * share_bps, BPS))
            ),
            without_provider_data=tuple(
                (provider, len(ids)) for provider, ids in sorted(self.no_data.items())
            ),
            fee_unknown=tuple(sorted(self.fee_unknown)),
            fee_by_tariff=tuple(sorted(self.fee_by_tariff)),
        )


@dataclass(slots=True)
class _Cost:
    usd: Decimal = ZERO_USD
    unknown_calls: int = 0


class _Tally:
    """The exact sums of a row."""

    __slots__ = ("gift_units", "keys", "money")

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.gift_units = 0
        self.money: dict[str, _Money] = defaultdict(_Money)

    def add(self, debit: ReviewDebit, batches: dict[uuid.UUID, _Batch]) -> None:
        self.keys.add(debit.action_key)
        self.gift_units += debit.gift_units
        if debit.currency is not None:
            self.money[debit.currency].add(debit, batches[debit.batch_id])

    def figures(self, costs: dict[str, _Cost], share_bps: int | None) -> PartnerFigures:
        usd, unknown = ZERO_USD, 0
        for key in self.keys:
            cost = costs.get(key)
            if cost is not None:
                usd += cost.usd
                unknown += cost.unknown_calls
        return PartnerFigures(
            reviews=len(self.keys),
            gift_units=self.gift_units,
            model_cost_usd=usd,
            unknown_cost_calls=unknown,
            money=tuple(
                money.line(currency, share_bps)
                for currency, money in sorted(self.money.items())
            ),
        )


# ── Reads ──────────────────────────────────────────────────────────────


def _fee(
    given: int | None,
    tariff: FeeTariff | None,
    amount_paid_minor: int | None,
    currency: str | None,
) -> tuple[int | None, bool]:
    """A batch's fee and whether it is the tariff's: the provider's has
    precedence (module docstring)."""
    if given is not None:
        return given, False
    if tariff is None or amount_paid_minor is None or currency is None:
        return None, False
    by_tariff = tariff.fee_minor(amount_paid_minor, currency)
    return by_tariff, by_tariff is not None


async def _batches(
    session: AsyncSession, ids: set[uuid.UUID], tariffs: Mapping[str, FeeTariff]
) -> dict[uuid.UUID, _Batch]:
    if not ids:
        return {}
    rows = await session.execute(
        select(
            BillingBatch.id,
            BillingBatch.provider,
            BillingBatch.units,
            BillingBatch.amount_paid_minor,
            BillingBatch.currency,
            BillingBatchProviderFigures.tax_minor,
            BillingBatchProviderFigures.fee_minor,
        )
        .outerjoin(
            BillingBatchProviderFigures,
            BillingBatchProviderFigures.batch_id == BillingBatch.id,
        )
        .where(BillingBatch.id.in_(ids))
    )
    found: dict[uuid.UUID, _Batch] = {}
    for batch_id, provider, units, paid, currency, tax, given in rows.tuples():
        name = provider or ""
        fee, by_tariff = _fee(given, tariffs.get(name), paid, currency)
        found[batch_id] = _Batch(name, units, tax, fee, by_tariff)
    return found


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _model_costs(
    session: AsyncSession, tenant_id: uuid.UUID, keys: set[str]
) -> dict[str, _Cost]:
    """What the models cost for each review, by its key (module docstring)."""
    submissions = {found for key in keys if (found := _uuid(key)) is not None}
    if not submissions:
        return {}
    known = ExternalServiceCall.cost_kind.in_(
        (CostKind.EXACT.value, CostKind.ESTIMATED.value)
    )
    rows = await session.execute(
        select(
            Job.subject_id,
            func.sum(ExternalServiceCall.cost_usd).filter(known),
            func.count(ExternalServiceCall.id).filter(
                ExternalServiceCall.cost_kind == CostKind.UNKNOWN.value
            ),
        )
        .select_from(ExternalServiceCall)
        .join(Job, Job.id == ExternalServiceCall.job_id)
        .join(HomeworkSubmission, HomeworkSubmission.id == Job.subject_id)
        .where(
            Job.job_type == JobType.HOMEWORK_PROCESSING.value,
            Job.subject_id.in_(submissions),
            HomeworkSubmission.tenant_id == tenant_id,
            ExternalServiceCall.cost_kind.is_not(None),
            ExternalServiceCall.action.notin_(PREPARATION_STAGES),
        )
        .group_by(Job.subject_id)
    )
    return {
        str(subject): _Cost(Decimal(str(usd)) if usd is not None else ZERO_USD, int(n))
        for subject, usd, n in rows.tuples()
    }


async def _own_courses(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(CourseNode.id, CourseNode.title).where(
            CourseNode.tenant_id == tenant_id, CourseNode.id.in_(ids)
        )
    )
    return dict(rows.tuples().all())


async def _refunds(
    session: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
) -> tuple[RefundLine, ...]:
    """Refunds approved and disputes lost on the school's students' accounts."""
    rows = await session.execute(
        select(
            BillingRefund.kind,
            BillingRefund.currency,
            func.count(BillingRefund.id),
            func.sum(BillingRefund.units),
            func.sum(BillingRefund.amount_minor),
        )
        .join(BillingAccount, BillingAccount.id == BillingRefund.account_id)
        .outerjoin(Student, Student.id == BillingAccount.student_id)
        .where(
            BillingAccount.tenant_id == tenant_id,
            BillingAccount.kind == AccountKind.STUDENT.value,
            Student.is_test_student.is_not(True),
            BillingRefund.state == RefundState.APPROVED.value,
            BillingRefund.decided_at >= start,
            BillingRefund.decided_at < end,
        )
        .group_by(BillingRefund.kind, BillingRefund.currency)
        .order_by(BillingRefund.kind, BillingRefund.currency)
    )
    return tuple(
        RefundLine(kind, currency, int(n), int(units), int(amount))
        for kind, currency, n, units, amount in rows.tuples()
    )


# ── The report ─────────────────────────────────────────────────────────


async def partner_report(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    month: date,
    fee_tariffs: Mapping[str, FeeTariff] | None = None,
) -> PartnerReport:
    """The partner's report of the school for the month of ``month``.

    ``fee_tariffs`` — the providers' fee tariffs by provider; None: the
    catalog's (``provider_fees``, module docstring).

    Raises:
        BillingNotFoundError: no such live tenant.
    """
    tariffs = get_catalog().provider_fees if fee_tariffs is None else fee_tariffs
    settings = await get_author_settings(session, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None  # get_author_settings found it live
    from_date, to_date = month_bounds(month)
    start, end = utc_bounds(from_date, to_date)
    share = settings.partner_share_bps

    debits = await review_debits(
        session,
        start_dt=start,
        end_dt_exclusive=end,
        tenant_id=tenant_id,
        include_test_students=False,
    )
    batches = await _batches(
        session, {d.batch_id for d in debits if d.currency is not None}, tariffs
    )
    costs = await _model_costs(session, tenant_id, {d.action_key for d in debits})
    links = await link_actions(
        session, {ActionRef(ActionKind.REVIEW, d.action_key) for d in debits}
    )
    courses = await _own_courses(
        session,
        tenant_id,
        {c for link in links.values() if (c := link.course_node_id) is not None},
    )

    totals, unlinked = _Tally(), _Tally()
    by_course: dict[uuid.UUID, _Tally] = defaultdict(_Tally)
    by_task: dict[uuid.UUID, dict[uuid.UUID, _Tally]] = defaultdict(
        lambda: defaultdict(_Tally)
    )
    for debit in debits:
        totals.add(debit, batches)
        link = links[ActionRef(ActionKind.REVIEW, debit.action_key)]
        course = link.course_node_id
        if course is None or course not in courses:
            unlinked.add(debit, batches)
            continue
        by_course[course].add(debit, batches)
        if link.authored_document_id is not None:
            by_task[course][link.authored_document_id].add(debit, batches)

    titles = await task_titles(
        session, {task for tasks in by_task.values() for task in tasks}
    )
    return PartnerReport(
        tenant_id=tenant_id,
        tenant_name=tenant.name,
        from_date=from_date,
        to_date=to_date,
        share_bps=share,
        courses=tuple(
            PartnerCourse(
                course_node_id=course,
                title=courses[course],
                figures=tally.figures(costs, share),
                tasks=tuple(
                    PartnerTask(
                        task, titles.get(task), task_tally.figures(costs, share)
                    )
                    for task, task_tally in sorted(
                        by_task[course].items(),
                        key=lambda item: (titles.get(item[0]) or "", str(item[0])),
                    )
                ),
            )
            for course, tally in sorted(
                by_course.items(),
                key=lambda item: (courses[item[0]] or "", str(item[0])),
            )
        ),
        unlinked=unlinked.figures(costs, share),
        totals=totals.figures(costs, share),
        refunds=await _refunds(session, tenant_id, start, end),
        excluded_test_reviews=await excluded_test_reviews(
            session, start_dt=start, end_dt_exclusive=end, tenant_id=tenant_id
        ),
    )
