"""Billing administration CLI (BIL-A, BIL-B): the administrator's only door.

By operator decision of 2026-11-05 there are no administrator routes until
there is a decision on how an administrator signs in; this script calls the
same service layer (``course_supporter.billing.admin`` / ``.accounts`` /
``.ledger``) that those routes will call. Every change writes an audit entry
naming ``--actor`` (required on every command that changes something), in the
same transaction as the change.

Usage (inside the application container, like ``scripts/manage_tenant.py``)::

    docker exec -it course-supporter-app python -m scripts.billing_admin <command> ...
    uv run python -m scripts.billing_admin <command> ...              # locally

``--tenant`` takes the tenant's id or its name. Ids of courses, students,
accounts and rules are UUIDs (``list-rules`` and ``show-account`` print them).

Read:
    show-account --account <account-id>
    show-account --tenant "Python Academy"                       # author account
    show-account --tenant "Python Academy" --student <student-id>
    get-settings --tenant "Python Academy"
    list-rules   --tenant "Python Academy" [--all]

Author settings (benefit, credit):
    set-benefit --tenant "Python Academy" --benefit monthly_limit --limit 500 \\
        --actor kostiantyn
    set-benefit --tenant "Python Academy" --benefit unlimited --actor kostiantyn
    set-benefit --tenant "Python Academy" --benefit none --actor kostiantyn
    set-credit  --tenant "Python Academy" --allowed yes --actor kostiantyn

Payment rules (the payer is the tenant's author account unless --payer names
it; a student account is refused):
    create-rule --tenant "Python Academy" --scope course --course <root-node-id> \\
        --action review --actor kostiantyn       # reviews on the course: author
    create-rule --tenant "Python Academy" --scope student --student <student-id> \\
        --action review --actor kostiantyn       # this student's reviews: author
    end-rule --rule <rule-id> --actor kostiantyn

Units (BIL-B). A credit is a batch of its own: valid 6 months, never
refunded, repaying the account's difference first; --reason is required and
the credit is audited (batch.admin_credit). Without --student — the author's
(the tenant's) account; the account is created on first need:
    credit  --tenant "Python Academy" --units 500 --reason "pilot" \
        --actor kostiantyn
    credit  --tenant "Python Academy" --student <student-id> --units 100 \
        --reason "a review lost to an outage" --actor kostiantyn
    balance --tenant "Python Academy" [--student <student-id>]
    statement --account <account-id> [--from 2026-11-01] [--to 2026-12-01]
        # movements in [from, to); dates or ISO instants, UTC

Contract invoices (BIL-PAY, B2B). A partner platform paid its invoice: the
units go to the tenant's author account as a purchase of provider "invoice"
— valid 6 months, repaying the account's difference first — and the credit
is audited (batch.invoice_credit). The invoice's number is the key: the same
number again changes nothing; with other figures it is refused:
    credit-invoice --tenant "Partner Academy" --units 10000 \
        --amount-minor 50000 --currency EUR --invoice INV-2026-0042 \
        --actor kostiantyn

Test purchases (BIL-PAY, operator 2026-11-15). While the payment provider is
in its test mode (on production, for the provider's review of the shop) a
test card pays and real units are credited, so only an account marked here
may buy: the provider's reviewer's demo author, our own test accounts. Without
--student — the author's (the tenant's) account; the account is created on
first need. Audited (account.test_purchase); the same mark again changes
nothing. In live mode the mark is not read:
    test-purchase --tenant "Demo Academy" --allow --actor kostiantyn
    test-purchase --tenant "Demo Academy" --student <student-id> --allow \
        --actor kostiantyn
    test-purchase --tenant "Demo Academy" --deny --actor kostiantyn

Test purchases' units (BIL-TESTMARK, operator 2026-11-16). A purchase paid in
the provider's test mode is marked as it is credited (billing_test_batches;
every Creem purchase before the marks was marked by their migration). Before
charging is switched on, one command takes the FREE units of every marked
batch out — a test_voided movement per batch, audited (batch.test_void,
before/after: free_units) — and prints what stayed reserved or held for a
refund or a dispute (untouched; a later run takes what has become free).
A second run changes nothing. --account limits it to one account; --dry-run
prints the same and writes nothing (a READ ONLY transaction). show-account
prints an account's test units:
    void-test-batches --actor kostiantyn [--account <account-id>] [--dry-run]

Test students (BIL-5, DD-BIL-CAL). Our own service students — control,
rehearsal, a payment provider reviewer's students — are marked on the student
(students.is_test_student): the calibration of the review cost estimate
(scripts/cost_estimate_report.py) leaves their reviews out. Nothing on the
review path reads the mark. --mark / --unmark print the mark before and after;
the same mark again changes nothing; an unknown or deleted student is refused.
Not in the audit log (its five-year destruction follows billing accounts, and
the mark is not on one): the change is a structured log line with the
--actor. --list only reads: the marked students' ids and tenants, and their
number:
    test-student --student <student-id> --mark --actor kostiantyn
    test-student --student <student-id> --unmark --actor kostiantyn
    test-student --list

Disputes (BIL-PAY, operator 2026-11-12). A chargeback held the free units of
the disputed purchase; a provider that does not report the outcome (Creem
sends nothing when a dispute is won) leaves it here. Lost: the held units
leave the batch. Won: they are free again (repaying a difference first). Units
spent before the dispute are the platform's loss; no debt arises. Audited as
dispute.lost / dispute.won; ending it again the same way changes nothing.
--provider is needed only when two providers share the id:
    dispute-lost --dispute disp_6vSsOdTANP5PhOzuDlUuXE --actor kostiantyn
    dispute-won  --dispute disp_6vSsOdTANP5PhOzuDlUuXE [--provider creem] \
        --actor kostiantyn

Refunds a user asked for (BIL-E, operator 2026-11-14/15). Creem's API makes
no refunds: they are made in its dashboard. First hold the units of the
purchase by its ORDER (Creem's order id, ord_…) — only free units of a valid
batch — then refund the money at Creem; its refund.created takes
ceil(amount refunded x units of the batch ÷ amount paid) units, no more than
held, and frees the rest of the hold. Without the hold the same event takes
the units by itself — the hold keeps them from being spent in between.
Audited as refund.order_hold / refund.order_release:
    refund-hold    --order ord_6vSsOdTANP5PhOzuDlUuXE --units 250 \
        [--provider creem] --reason "the user asked, ticket 42" --actor kostiantyn
    refund-release --order ord_6vSsOdTANP5PhOzuDlUuXE [--provider creem] \
        --actor kostiantyn      # the refund did not happen

Anonymization, AFTER the tenant or the student was deleted by hand. A
student's deletion at their request (``scripts/student_admin.py delete``, Б-2)
calls the same function itself; there is no tenant deletion flow yet.
It closes and anonymizes the owner's accounts and ends the active payment
rules about the owner — a student's student rules, every rule of a tenant —
each ending audited as payment_rule.end; a second run changes nothing:
    anonymize-student --student <student-id> --actor kostiantyn
    anonymize-tenant  --tenant <tenant-id> --actor kostiantyn

Periodic tasks (BIL-B part 2), run by hand — the same run the scheduler makes
(``course_supporter.periodic``): the task's advisory lock, its transaction, its
log line. For a deployment check and for diagnosis; a task switched off in the
settings runs all the same. --dry-run does the work in a transaction that is
rolled back: it counts what a run would do now and changes nothing. Nothing is
audited (the scheduler's runs are not either), so there is no --actor:
    run-periodic --task billing_expire [--dry-run]     # burn expired units
    run-periodic --task billing_annul [--dry-run]      # annul differences
    run-periodic --task billing_stale_reserves [--dry-run]
        # release reserves hanging longer than BILLING_STALE_RESERVE_HOURS
    run-periodic --task billing_purge --dry-run
        # count what the five-year destruction would delete now

Costs (BIL-COSTS). Real platform external service spend (USD) by author
(tenant) and by student for an inclusive UTC calendar interval. Read-only;
no writes, no audit entries, and no --actor (defaults: current UTC month
up to today, all tenants, --by both, --format table):
    costs
    costs --from 2026-11-01 --to 2026-11-30 --tenant "Python Academy"
    costs --by student --format csv
    costs --from 2026-11-01 --to 2026-11-15 --by author
Beside the dollars, the units from the journal (BIL-E2): billed (debit +
difference) to the author — "виставлено_авторові" — and to the tenant's
students — "виставлено_студентам", and "покрито_юнітів" (borne by the benefit
or the platform). The dollars are dated by the call, the units by the movement:
at the period's edge they can part (the report's head says so). --by task
breaks it down author → course → homework task (processing is the course's):
    costs --by task --from 2026-11-01 --to 2026-11-30 [--format csv]
The students' side of the reviews (Б-2, operator 2026-11-24), in the author and
task rows: reviews, units_per_review, students_money_minor and gift_units
(``costs --help`` says what each is); the service students
(students.is_test_student) are left out of them and of the students' units, and
the report says how many reviews that was, until --include-test-students:
    costs --by author --include-test-students

The partner's report (Б-3, operator 2026-11-27). INTERNAL: what of it a
school is shown is the product owner's decision. The school's share rate is
kept in its author settings — a percent with at most two decimals, 0 to 100,
or none; audited as author_settings.update, the same rate again writes
nothing:
    set-partner-share --tenant "Python Academy" --percent 12.5 --actor kostiantyn
    set-partner-share --tenant "Python Academy" --percent none --actor kostiantyn
The tax, the refunded part and the fee of Creem's purchases go on their batches
with the purchase's webhook (the tax) and by asking Creem's API for the paid
batches created in a period — Creem's key from the application's settings
(CREEM_API_KEY, CREEM_API_BASE). Creem's API gives no fee: it stays unknown.
Each batch changed is audited (batch.provider_sync); a second run changes
nothing; --dry-run asks the API and only counts:
    creem-sync --from 2026-11-01 --to 2026-11-30 --actor kostiantyn [--dry-run]
A month of the school (UTC, both edges inclusive), by course -> task and in
total, each currency apart: reviews its students paid from their own accounts
(service students left out), gross_minor (their money spent on those reviews,
tax included — units bought and not used are not in it), tax_minor and
fee_minor (each batch's in proportion to the units spent; a batch Creem has
not told the tax of is counted "без даних Creem"; the fee Creem does not give:
"комісія Creem: API не віддає"), net_minor = gross - tax - fee,
model_cost_usd (what the models cost for these reviews), the share at the
school's rate ("ставку не задано" without one), and the month's refunds to the
school's students on a line apart. Read-only (``partner-report --help`` says
what each field is):
    partner-report --tenant "Python Academy" --month 2026-11 [--format text|csv|json]

Exit status: 0 on success; 1 when the operation is refused (the message says
why; nothing is written) or a periodic task failed or found its lock held;
2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import io
import json
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from course_supporter.billing import (
    accounts,
    admin,
    audit,
    ledger,
    partner_report,
    provider_figures,
    testmark,
)
from course_supporter.billing import costs as billing_costs
from course_supporter.billing.errors import (
    BillingError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.kinds import ActionKind, Benefit, MovementKind, RuleScope
from course_supporter.billing.payments import invoice
from course_supporter.billing.payments.creem import CREEM, CreemProvider
from course_supporter.billing.payments.port import TransactionLookup
from course_supporter.billing.payments.registry import PaymentConfigError, adapters
from course_supporter.billing.periodic import BILLING_TASKS
from course_supporter.config import get_settings
from course_supporter.periodic.runner import TaskRun, run_task
from course_supporter.storage.orm import (
    BillingAccount,
    BillingMovement,
    BillingPaymentRule,
    Student,
    Tenant,
)

logger = structlog.get_logger(__name__)

Handler = Callable[[AsyncSession, argparse.Namespace], Awaitable[str]]


# ── Arguments ──────────────────────────────────────────────────────────


async def resolve_tenant_id(session: AsyncSession, value: str) -> uuid.UUID:
    """A tenant id from an id or a name."""
    try:
        return uuid.UUID(value)
    except ValueError:
        pass
    tenant_id = (
        await session.execute(select(Tenant.id).where(Tenant.name == value))
    ).scalar_one_or_none()
    if tenant_id is None:
        raise BillingNotFoundError(f"tenant {value!r} not found")
    return tenant_id


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _instant(value: str) -> datetime:
    """A date or an ISO instant; without a zone it is UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _utc_date(value: str) -> date:
    """Parse a strict YYYY-MM-DD calendar date."""
    if _DATE_RE.match(value) is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


def _yes_no(value: str) -> bool:
    if value not in ("yes", "no"):
        raise argparse.ArgumentTypeError("yes or no")
    return value == "yes"


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _month(value: str) -> date:
    """A strict YYYY-MM month: its first day."""
    if _MONTH_RE.match(value) is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM month: {value!r}")
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM month: {value!r}") from exc


def _percent(value: str) -> int | None:
    """A percent with at most two decimals, in hundredths (bps); ``none`` — None.

    The range (0 to 100) is the service's to refuse.
    """
    if value == "none":
        return None
    try:
        percent = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a percent: {value!r}") from exc
    if not percent.is_finite() or percent != percent.quantize(Decimal("0.01")):
        raise argparse.ArgumentTypeError(
            f"a percent with at most two decimals, or none: {value!r}"
        )
    return int(percent * 100)


def _rate(bps: int | None) -> str:
    """Hundredths of a per cent as a person reads them: ``12.5 %``."""
    if bps is None:
        return partner_report.NO_RATE
    return f"{Decimal(bps) / 100:f} %"


# ── Output ─────────────────────────────────────────────────────────────


def _account_line(account: BillingAccount) -> str:
    return (
        f"account {account.id}  kind={account.kind}  status={account.status}  "
        f"tenant={account.tenant_id}  student={account.student_id}  "
        f"created={account.created_at:%Y-%m-%d}  closed={account.closed_at}  "
        f"anonymized={account.anonymized_at}  "
        f"test_purchase={'yes' if account.test_purchase_allowed else 'no'}"
    )


def _rule_line(rule: BillingPaymentRule) -> str:
    target = rule.course_node_id if rule.scope == RuleScope.COURSE else rule.student_id
    state = "active" if rule.active else f"ended {rule.ended_at:%Y-%m-%d %H:%M}"
    return (
        f"rule {rule.id}  {rule.action_kind} on {rule.scope} {target}  "
        f"payer={rule.payer_account_id}  {state}  by {rule.created_by}"
    )


def _settings_line(view: admin.AuthorSettingsView) -> str:
    units = view.monthly_limit_units
    limit = f" ({units} units/month)" if units is not None else ""
    share = "none" if view.partner_share_bps is None else _rate(view.partner_share_bps)
    return (
        f"author account {view.account_id or 'none yet'}  "
        f"benefit={view.benefit.value}{limit}  "
        f"since={view.benefit_started_at}  credit_allowed={view.credit_allowed}  "
        f"partner_share={share}"
    )


def _balance_line(account_id: uuid.UUID, view: ledger.Balance) -> str:
    expiry = f"{view.next_expiry:%Y-%m-%d}" if view.next_expiry else "-"
    return (
        f"account {account_id}  balance={view.balance}  reserved={view.reserved}  "
        f"held={view.held}  available={view.available}  "
        f"difference={view.difference}  next_expiry={expiry}"
    )


MOVEMENT_WORDS: dict[MovementKind, str] = {
    MovementKind.CREDIT: "units arrived (a purchase or a credit)",
    MovementKind.RESERVE: "held for an action at its start",
    MovementKind.RESERVE_RELEASE: "free again: the rest of a reserve",
    MovementKind.DEBIT: "charged for an action",
    MovementKind.DIFFERENCE_ARISEN: "owed: the action cost more than was there",
    MovementKind.DIFFERENCE_REPAID: "repaid what was owed",
    MovementKind.BATCH_EXPIRED: "burned: the batch expired",
    MovementKind.DIFFERENCE_ANNULLED: "owed no more: the difference annulled",
    MovementKind.BENEFIT_COVERED: "covered by the author's benefit",
    MovementKind.PLATFORM_COVERED: (
        "covered by the platform: the cost over the reserve, or a re-processing"
    ),
    MovementKind.REFUND_HOLD: "held for a refund or a dispute",
    MovementKind.REFUND_COMPLETED: "refunded: gone from the batch",
    MovementKind.REFUND_REJECTED: "free again: the refund or hold did not happen",
    MovementKind.TEST_VOIDED: "taken out: the free units of a test purchase",
}
"""What each kind of movement means, for a person reading a statement (BIL-E)."""


def _movement_line(m: BillingMovement) -> str:
    action = f"{m.action_kind}:{m.action_key}" if m.action_kind else "-"
    words = MOVEMENT_WORDS.get(MovementKind(m.kind), m.kind)
    return (
        f"{m.created_at:%Y-%m-%d %H:%M:%S}  {m.kind:<19} {m.units:>7}  "
        f"({words})  "
        f"batch={m.batch_id or '-'}  action={action}  "
        f"for={m.beneficiary_account_id or '-'}  source={m.payer_source or '-'}  "
        f"rule={m.rule_id or '-'}  actor={m.actor or '-'}"
    )


# ── Commands ───────────────────────────────────────────────────────────


async def show_account(session: AsyncSession, args: argparse.Namespace) -> str:
    """One account in full, or every account of an owner."""
    if args.account is not None:
        found = [args.account]
    else:
        if args.tenant is None:
            raise BillingNotFoundError("give --account, or --tenant [--student]")
        tenant_id = await resolve_tenant_id(session, args.tenant)
        owned = await admin.find_accounts(
            session, tenant_id=tenant_id, student_id=args.student
        )
        if not owned:
            return "no account yet (it is created on first need)"
        found = [a.id for a in owned]
    lines: list[str] = []
    for account_id in found:
        view = await admin.read_account(session, account_id)
        lines.append(_account_line(view.account))
        if view.settings is not None:
            lines.append("  " + _settings_line(view.settings))
        lines.extend("  pays under " + _rule_line(r) for r in view.rules_paid)
        lines.extend(
            f"  consent {c.terms_version} at {c.consented_at:%Y-%m-%d %H:%M} "
            f"via {c.channel}"
            for c in view.consents
        )
        marked = await testmark.marked_count(session, account_id)
        if marked:
            free = await testmark.free_test_units(session, account_id)
            lines.append(
                f"  test units={free}  (free units of {marked} batch(es) marked "
                "as test purchases; void-test-batches takes them out)"
            )
        else:
            lines.append("  test units=0  (no batch marked as a test purchase)")
    return "\n".join(lines)


async def get_settings_cmd(session: AsyncSession, args: argparse.Namespace) -> str:
    """The author settings of a tenant."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    return _settings_line(await admin.get_author_settings(session, tenant_id))


async def set_benefit(session: AsyncSession, args: argparse.Namespace) -> str:
    """Grant or withdraw a benefit."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    view = await admin.update_author_settings(
        session,
        tenant_id,
        actor=args.actor,
        benefit=Benefit(args.benefit),
        monthly_limit_units=args.limit,
    )
    return _settings_line(view)


async def set_credit(session: AsyncSession, args: argparse.Namespace) -> str:
    """Allow or forbid credit."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    view = await admin.update_author_settings(
        session, tenant_id, actor=args.actor, credit_allowed=args.allowed
    )
    return _settings_line(view)


async def set_partner_share(session: AsyncSession, args: argparse.Namespace) -> str:
    """Set or remove the school's share rate (the partner's report)."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    view = await admin.update_author_settings(
        session, tenant_id, actor=args.actor, partner_share_bps=args.percent
    )
    return _settings_line(view)


async def list_rules(session: AsyncSession, args: argparse.Namespace) -> str:
    """The tenant's payment rules."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    rules = await admin.list_payment_rules(session, tenant_id, include_ended=args.all)
    return "\n".join(_rule_line(r) for r in rules) or "no rules"


async def create_rule(session: AsyncSession, args: argparse.Namespace) -> str:
    """Set a payment rule."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    rule = await admin.create_payment_rule(
        session,
        actor=args.actor,
        tenant_id=tenant_id,
        scope=RuleScope(args.scope),
        action_kind=ActionKind(args.action),
        course_node_id=args.course,
        student_id=args.student,
        payer_account_id=args.payer,
    )
    return "created " + _rule_line(rule)


async def end_rule(session: AsyncSession, args: argparse.Namespace) -> str:
    """End a payment rule."""
    rule = await admin.end_payment_rule(session, args.rule, actor=args.actor)
    return "ended " + _rule_line(rule)


async def credit(session: AsyncSession, args: argparse.Namespace) -> str:
    """Credit units to an owner's account: a batch of its own, audited."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    account = await admin.owner_account(
        session,
        tenant_id=tenant_id,
        student_id=args.student,
        create=True,
        actor=args.actor,
    )
    assert account is not None  # created on first need
    done = await ledger.credit_admin(
        session,
        account_id=account.id,
        units=args.units,
        reason=args.reason,
        actor=args.actor,
    )
    batch = done.batch
    return (
        f"credited batch {batch.id}  {batch.units} units to account {account.id}  "
        f"expires={batch.expires_at:%Y-%m-%d}  repaid_difference={done.repaid_units}"
        "\n" + _balance_line(account.id, await ledger.balance(session, account.id))
    )


async def balance(session: AsyncSession, args: argparse.Namespace) -> str:
    """The balance of an owner's account."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    account = await admin.owner_account(
        session, tenant_id=tenant_id, student_id=args.student, create=False
    )
    if account is None:
        return "no account yet (it is created on first need)"
    return _balance_line(account.id, await ledger.balance(session, account.id))


async def statement(session: AsyncSession, args: argparse.Namespace) -> str:
    """The movements of an account in [from, to)."""
    if await session.get(BillingAccount, args.account) is None:
        raise BillingNotFoundError(f"account {args.account} not found")
    rows = await ledger.statement(
        session, args.account, since=args.since, until=args.until
    )
    return "\n".join(_movement_line(m) for m in rows) or "no movements"


async def mark_test_purchase(session: AsyncSession, args: argparse.Namespace) -> str:
    """Mark or unmark an owner's account for test purchases, audited."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    done = await admin.set_test_purchase(
        session,
        tenant_id=tenant_id,
        student_id=args.student,
        allowed=args.allowed,
        actor=args.actor,
    )
    state = "changed" if done.changed else "unchanged (already so)"
    return f"{state}\n" + _account_line(done.account)


async def credit_invoice(session: AsyncSession, args: argparse.Namespace) -> str:
    """Credit a paid contract invoice (B2B) to the tenant's author account."""
    tenant_id = await resolve_tenant_id(session, args.tenant)
    done = await invoice.credit_invoice(
        session,
        tenant_id=tenant_id,
        units=args.units,
        amount_minor=args.amount_minor,
        currency=args.currency,
        invoice=args.invoice,
        actor=args.actor,
    )
    batch = done.batch
    head = (
        f"credited invoice {batch.provider_transaction_id}: batch {batch.id}  "
        f"{batch.units} units to account {batch.account_id}  "
        f"amount={batch.amount_paid_minor} {batch.currency}  "
        f"expires={batch.expires_at:%Y-%m-%d}  repaid_difference={done.repaid_units}"
        if done.created
        else f"invoice {batch.provider_transaction_id} was credited before as "
        f"batch {batch.id}; nothing changed"
    )
    return (
        head
        + "\n"
        + _balance_line(
            batch.account_id, await ledger.balance(session, batch.account_id)
        )
    )


def _void_lines(run: testmark.VoidRun) -> str:
    """One line per marked batch, then the totals; no owner's id."""
    lines = [
        f"account {b.account_id} ({b.account_kind})  batch {b.batch_id}  "
        f"voided={b.voided}  left reserved={b.reserved}  left held={b.held}"
        for b in run.batches
    ]
    lines.append(
        f"total: {len(run.batches)} marked batch(es) on {run.accounts} "
        f"account(s)  voided={run.voided}  left reserved={run.reserved}  "
        f"left held={run.held}"
    )
    if run.dry_run:
        lines.append("dry run: nothing written")
    return "\n".join(lines)


async def void_test_batches(session: AsyncSession, args: argparse.Namespace) -> str:
    """Take the free units of the batches marked as test purchases out."""
    if args.dry_run:
        await session.execute(text("SET TRANSACTION READ ONLY"))
    if (
        args.account is not None
        and await session.get(BillingAccount, args.account) is None
    ):
        raise BillingNotFoundError(f"account {args.account} not found")
    done = await testmark.void_test_batches(
        session, actor=args.actor, account_id=args.account, dry_run=args.dry_run
    )
    if not done.batches:
        return "no batch marked as a test purchase" + (
            "\ndry run: nothing written" if done.dry_run else ""
        )
    return _void_lines(done)


async def test_student(session: AsyncSession, args: argparse.Namespace) -> str:
    """Mark or unmark a service student, or list the marked ones (BIL-5).

    The mark (``students.is_test_student``) leaves the student's reviews out
    of the calibration of the review cost estimate; the review path does not
    read it. ``--list`` only reads: the marked students' ids and tenants (no
    name), in a READ ONLY transaction. A change is logged with ``--actor``
    (``student_test_mark_set``), not audited: the audit log's destruction
    follows billing accounts, and the mark is not on one.
    """
    if args.list:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        marked = (
            await session.execute(
                select(Student.id, Student.tenant_id, Student.deleted_at)
                .where(Student.is_test_student)
                .order_by(Student.tenant_id, Student.id)
            )
        ).all()
        lines = [
            f"student {sid}  tenant {tid}" + ("  deleted" if deleted else "")
            for sid, tid, deleted in marked
        ]
        lines.append(f"total: {len(marked)} marked student(s)")
        return "\n".join(lines)
    actor = audit.require_actor(args.actor)
    student = await session.get(Student, args.student, with_for_update=True)
    if student is None or student.deleted_at is not None:
        raise BillingNotFoundError(f"student {args.student} not found")
    before = student.is_test_student
    if before is args.mark:
        return f"unchanged (already so): student {student.id}  is_test_student={before}"
    student.is_test_student = args.mark
    await session.flush()
    logger.info(
        "student_test_mark_set",
        actor=actor,
        student_id=str(student.id),
        before=before,
        after=args.mark,
    )
    return f"changed: student {student.id}  is_test_student {before} → {args.mark}"


async def _end_dispute(
    session: AsyncSession, args: argparse.Namespace, *, lost: bool
) -> str:
    refund, changed = await admin.end_dispute(
        session,
        dispute_id=args.dispute,
        provider=args.provider,
        lost=lost,
        actor=args.actor,
    )
    outcome = "lost: units gone" if lost else "won: units free again"
    head = (
        f"dispute {refund.provider_adjustment_id} ({refund.provider}) {outcome}  "
        f"units={refund.units}  batch {refund.batch_id}"
        if changed
        else f"dispute {refund.provider_adjustment_id} ({refund.provider}) was "
        f"already {'lost' if lost else 'won'}; nothing changed"
    )
    return (
        head
        + "\n"
        + _balance_line(
            refund.account_id, await ledger.balance(session, refund.account_id)
        )
    )


async def refund_hold(session: AsyncSession, args: argparse.Namespace) -> str:
    """Hold units of an order's purchase before refunding it at the provider."""
    refund, created = await admin.hold_refund_by_order(
        session,
        provider=args.provider,
        order_id=args.order,
        units=args.units,
        reason=args.reason,
        actor=args.actor,
    )
    head = (
        f"held {refund.units} units of {refund.provider} order {args.order}  "
        f"batch {refund.batch_id}  (refund {refund.amount_minor} "
        f"{refund.currency} minor units at the provider now)"
        if created
        else f"{refund.provider} order {args.order} was already held for "
        f"{refund.units} units; nothing changed"
    )
    return (
        head
        + "\n"
        + _balance_line(
            refund.account_id, await ledger.balance(session, refund.account_id)
        )
    )


async def refund_release(session: AsyncSession, args: argparse.Namespace) -> str:
    """The refund did not happen: the order's held units are free again."""
    refund = await admin.release_refund_by_order(
        session, provider=args.provider, order_id=args.order, actor=args.actor
    )
    return (
        f"released {refund.units} units of {refund.provider} order {args.order}\n"
        + _balance_line(
            refund.account_id, await ledger.balance(session, refund.account_id)
        )
    )


async def dispute_lost(session: AsyncSession, args: argparse.Namespace) -> str:
    """A dispute lost: the held units leave the batch."""
    return await _end_dispute(session, args, lost=True)


async def dispute_won(session: AsyncSession, args: argparse.Namespace) -> str:
    """A dispute won: the held units are free again."""
    return await _end_dispute(session, args, lost=False)


def _anonymization_lines(done: accounts.Anonymization) -> str:
    def ids(values: list[uuid.UUID]) -> str:
        return ", ".join(map(str, values)) or "-"

    return (
        f"anonymized {len(done.account_ids)} account(s): {ids(done.account_ids)}\n"
        f"ended {len(done.ended_rule_ids)} rule(s): {ids(done.ended_rule_ids)}"
    )


async def anonymize_student(session: AsyncSession, args: argparse.Namespace) -> str:
    """Close and anonymize a deleted student's accounts; end their rules."""
    done = await accounts.anonymize_student_accounts(
        session, args.student, actor=args.actor
    )
    return _anonymization_lines(done)


async def anonymize_tenant(session: AsyncSession, args: argparse.Namespace) -> str:
    """Close and anonymize a deleted tenant's accounts; end its rules."""
    done = await accounts.anonymize_tenant_accounts(
        session, args.tenant, actor=args.actor
    )
    return _anonymization_lines(done)


class PeriodicRunError(Exception):
    """A periodic task run by hand failed or found its lock held."""


def _periodic_line(run: TaskRun) -> str:
    details = "".join(f", {k}={v}" for k, v in run.details.items())
    dry = " (dry run: nothing changed)" if run.dry_run else ""
    return (
        f"{run.task}: processed {run.processed}{details} in {run.duration_ms} ms{dry}"
    )


async def run_periodic(args: argparse.Namespace) -> str:
    """Run one periodic task as the scheduler would (module docstring)."""
    task = next(t for t in BILLING_TASKS if t.name == args.task)
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        done = await run_task(task, factory, settings=settings, dry_run=args.dry_run)
    finally:
        await engine.dispose()
    if done.skipped == "busy":
        raise PeriodicRunError(f"{task.name}: another run holds its lock; try later")
    if done.error is not None:
        raise PeriodicRunError(f"{task.name} failed: {done.error}")
    line = _periodic_line(done)
    if not task.enabled(settings):
        line += f"\nnote: {task.name} is switched off in the schedule"
    return line


def _format_decimal_usd(value: Decimal) -> str:
    if value == billing_costs.ZERO_USD:
        return "0.000000"
    fixed12 = f"{value:.12f}".rstrip("0")
    whole, _, frac = fixed12.partition(".")
    if len(frac) < 6:
        frac = frac.ljust(6, "0")
    return f"{whole}.{frac}"


def _fmt_usd(value: Decimal) -> str:
    return f"${_format_decimal_usd(value)}"


def _fmt_bucket(bucket: billing_costs.ActionSpend) -> str:
    base = f"{bucket.calls} / {_fmt_usd(bucket.known_usd)}"
    if bucket.unknown_calls > 0:
        return f"{base} (невідомо: {bucket.unknown_calls})"
    return base


def _format_ascii_table(
    headers: list[str],
    rows: list[list[str]],
    total_row: list[str],
    *,
    right_align: set[int],
) -> list[str]:
    all_rows = [headers, *rows, total_row]
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]

    def _render_row(cells: list[str]) -> str:
        formatted = [
            cell.rjust(widths[idx]) if idx in right_align else cell.ljust(widths[idx])
            for idx, cell in enumerate(cells)
        ]
        return "  ".join(formatted).rstrip()

    separator = "  ".join("-" * w for w in widths)
    lines = [_render_row(headers), separator]
    lines.extend(_render_row(row) for row in rows)
    lines.append(separator)
    lines.append(_render_row(total_row))
    return lines


def _render_author_table(report: billing_costs.CostReport) -> list[str]:
    has_other = report.authors_total.other.calls > 0
    headers = [
        "орендар_id",
        "орендар",
        "виклики",
        "сума_usd",
        _PLATFORM_HEADER,
        *_UNITS_HEADERS,
        *_REVIEW_HEADERS,
        "exact",
        "estimated",
        "unknown",
        "обробка",
        "підготовка завдань",
        "перевірка робіт студентів",
    ]
    if has_other:
        headers.append("інше")

    def _cells(
        tenant_id_str: str,
        tenant_name_str: str,
        spend: billing_costs.SpendSummary,
        processing: billing_costs.ActionSpend,
        prep: billing_costs.ActionSpend,
        review: billing_costs.ActionSpend,
        other: billing_costs.ActionSpend,
        units: billing_costs.BilledUnits,
        reviews: billing_costs.ReviewFigures,
    ) -> list[str]:
        cells = [
            tenant_id_str,
            tenant_name_str,
            str(spend.calls),
            _fmt_usd(spend.known_usd),
            _fmt_usd(spend.platform_usd),
            *_units_cells(units),
            *_reviews_cells(reviews),
            f"{spend.exact_calls} / {_fmt_usd(spend.exact_usd)}",
            f"{spend.estimated_calls} / {_fmt_usd(spend.estimated_usd)}",
            str(spend.unknown_calls),
            _fmt_bucket(processing),
            _fmt_bucket(prep),
            _fmt_bucket(review),
        ]
        if has_other:
            cells.append(_fmt_bucket(other))
        return cells

    rows = [
        _cells(
            str(row.tenant_id) if row.tenant_id is not None else "-",
            row.tenant_name or "-",
            row.spend,
            row.processing,
            row.task_preparation,
            row.review,
            row.other,
            row.units,
            row.reviews,
        )
        for row in report.authors
    ]
    totals = report.authors_total
    total_row = _cells(
        "разом",
        "",
        totals.spend,
        totals.processing,
        totals.task_preparation,
        totals.review,
        totals.other,
        totals.units,
        totals.reviews,
    )
    lines = ["По авторах:"]
    lines.extend(
        _format_ascii_table(
            headers,
            rows,
            total_row,
            right_align={2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14},
        )
    )
    lines.append(f"вартість невідома: {totals.spend.unknown_calls} викликів")
    lines.append(_excluded_line(report))
    return lines


def _render_student_table(report: billing_costs.CostReport) -> list[str]:
    headers = [
        "орендар_id",
        "орендар",
        "студент_id",
        "подачі",
        "виклики",
        "сума_usd",
        _PLATFORM_HEADER,
        *_UNITS_HEADERS,
        "exact",
        "estimated",
        "unknown",
    ]

    def _cells(
        tenant_id_str: str,
        tenant_name_str: str,
        student_id_str: str,
        submissions: int,
        spend: billing_costs.SpendSummary,
        units: billing_costs.BilledUnits,
    ) -> list[str]:
        return [
            tenant_id_str,
            tenant_name_str,
            student_id_str,
            str(submissions),
            str(spend.calls),
            _fmt_usd(spend.known_usd),
            _fmt_usd(spend.platform_usd),
            *_units_cells(units),
            f"{spend.exact_calls} / {_fmt_usd(spend.exact_usd)}",
            f"{spend.estimated_calls} / {_fmt_usd(spend.estimated_usd)}",
            str(spend.unknown_calls),
        ]

    rows = [
        _cells(
            str(row.tenant_id),
            row.tenant_name or "-",
            str(row.student_id),
            row.submissions,
            row.spend,
            row.units,
        )
        for row in report.students
    ]
    totals = report.students_total
    total_row = _cells("разом", "", "", totals.submissions, totals.spend, totals.units)
    lines = ["По студентах:"]
    lines.extend(
        _format_ascii_table(
            headers, rows, total_row, right_align={3, 4, 5, 6, 7, 8, 9, 12}
        )
    )
    lines.append(f"вартість невідома: {totals.spend.unknown_calls} викликів")
    lines.append(_excluded_line(report))
    return lines


_PLATFORM_HEADER = "платформа_usd"
"""Of the dollars, what no payer pays: retries, calls that failed on the
infrastructure, the reply screen (``costs.SpendSummary.platform_usd``)."""

_UNITS_HEADERS = ("виставлено_авторові", "виставлено_студентам", "покрито_юнітів")
"""Units billed to the author, to students, and borne by the benefit or the
platform (``costs.BilledUnits``)."""

_REVIEW_HEADERS = ("reviews", "units_per_review", "students_money_minor", "gift_units")
"""What the students paid for the reviews (``costs.ReviewFigures``)."""

NO_AVERAGE = "—"
"""``units_per_review`` of a row without reviews."""

NO_COURSE = "(без курсу)"
NO_TASK = "(без завдання)"


def _units_cells(units: billing_costs.BilledUnits) -> list[str]:
    return [
        str(units.author_units),
        str(units.students_units),
        str(units.covered_units),
    ]


def _money_cell(figures: billing_costs.ReviewFigures) -> str:
    """``EUR 1234; USD 500`` (minor units, one part per currency); ``0`` if none."""
    if not figures.money_minor:
        return "0"
    return "; ".join(f"{currency} {minor}" for currency, minor in figures.money_minor)


def _reviews_cells(figures: billing_costs.ReviewFigures) -> list[str]:
    per_review = figures.units_per_review
    return [
        str(figures.count),
        NO_AVERAGE if per_review is None else str(per_review),
        _money_cell(figures),
        str(figures.gift_units),
    ]


def _excluded_line(report: billing_costs.CostReport) -> str:
    """The summary line about the service students' reviews left out."""
    line = f"excluded: {report.excluded_test_reviews} reviews of test students"
    if report.include_test_students:
        line += " (--include-test-students)"
    return line


def _render_task_table(report: billing_costs.CostReport) -> list[str]:
    headers = [
        "орендар",
        "курс",
        "завдання",
        "виклики",
        "сума_usd",
        _PLATFORM_HEADER,
        *_UNITS_HEADERS,
        *_REVIEW_HEADERS,
        "unknown",
        "курс_id",
        "завдання_id",
    ]

    def _cells(
        tenant: str,
        course: str,
        task: str,
        spend: billing_costs.SpendSummary,
        units: billing_costs.BilledUnits,
        reviews: billing_costs.ReviewFigures,
        course_id: str,
        task_id: str,
    ) -> list[str]:
        return [
            tenant,
            course,
            task,
            str(spend.calls),
            _fmt_usd(spend.known_usd),
            _fmt_usd(spend.platform_usd),
            *_units_cells(units),
            *_reviews_cells(reviews),
            str(spend.unknown_calls),
            course_id,
            task_id,
        ]

    rows = [
        _cells(
            row.tenant_name or "-",
            (row.course_title or "-") if row.course_node_id else NO_COURSE,
            (row.task_title or "-") if row.authored_document_id else NO_TASK,
            row.spend,
            row.units,
            row.reviews,
            str(row.course_node_id) if row.course_node_id else "",
            str(row.authored_document_id) if row.authored_document_id else "",
        )
        for row in report.tasks
    ]
    totals = report.tasks_total
    total_row = _cells(
        "разом", "", "", totals.spend, totals.units, totals.reviews, "", ""
    )
    lines = ["По завданнях (автор → курс → завдання):"]
    lines.extend(
        _format_ascii_table(
            headers,
            rows,
            total_row,
            right_align={3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13},
        )
    )
    lines.append(f"вартість невідома: {totals.spend.unknown_calls} викликів")
    lines.append(_excluded_line(report))
    return lines


def _render_cost_table(
    report: billing_costs.CostReport, *, tenant_label: str | None
) -> str:
    tenant_text = tenant_label or "усі"
    lines = [
        (
            f"Період (UTC): {report.from_date.isoformat()} .. "
            f"{report.to_date.isoformat()} (включно) | Орендар: {tenant_text}"
        ),
        billing_costs.DATING_NOTE,
    ]
    if report.by in ("author", "both"):
        lines.append("")
        lines.extend(_render_author_table(report))
    if report.by in ("student", "both"):
        lines.append("")
        lines.extend(_render_student_table(report))
    if report.by == "task":
        lines.append("")
        lines.extend(_render_task_table(report))
    return "\n".join(lines)


_CSV_FIELDNAMES = (
    "section",
    "row_kind",
    "tenant_id",
    "tenant_name",
    "student_id",
    "submissions",
    "calls",
    "total_usd",
    "platform_usd",
    "exact_calls",
    "exact_usd",
    "estimated_calls",
    "estimated_usd",
    "unknown_calls",
    "processing_calls",
    "processing_usd",
    "task_preparation_calls",
    "task_preparation_usd",
    "review_calls",
    "review_usd",
    "other_calls",
    "other_usd",
    "billed_author_units",
    "billed_students_units",
    "covered_units",
    "reviews",
    "units_per_review",
    "students_money_minor",
    "gift_units",
    "excluded_test_reviews",
    "course_id",
    "course_title",
    "task_id",
    "task_title",
    "note",
)

_NO_TASK_CSV: dict[str, str] = {
    "course_id": "",
    "course_title": "",
    "task_id": "",
    "task_title": "",
}

_EMPTY_ACTION_CSV: dict[str, str] = {
    "processing_calls": "",
    "processing_usd": "",
    "task_preparation_calls": "",
    "task_preparation_usd": "",
    "review_calls": "",
    "review_usd": "",
    "other_calls": "",
    "other_usd": "",
}


def _spend_csv_fields(spend: billing_costs.SpendSummary) -> dict[str, str]:
    return {
        "calls": str(spend.calls),
        "total_usd": _format_decimal_usd(spend.known_usd),
        "platform_usd": _format_decimal_usd(spend.platform_usd),
        "exact_calls": str(spend.exact_calls),
        "exact_usd": _format_decimal_usd(spend.exact_usd),
        "estimated_calls": str(spend.estimated_calls),
        "estimated_usd": _format_decimal_usd(spend.estimated_usd),
        "unknown_calls": str(spend.unknown_calls),
    }


def _units_csv_fields(units: billing_costs.BilledUnits) -> dict[str, str]:
    return {
        "billed_author_units": str(units.author_units),
        "billed_students_units": str(units.students_units),
        "covered_units": str(units.covered_units),
    }


_NO_REVIEWS_CSV: dict[str, str] = {
    "reviews": "",
    "units_per_review": "",
    "students_money_minor": "",
    "gift_units": "",
}
"""The student rows have no figures of the reviews they are billed for."""


def _reviews_csv_fields(
    figures: billing_costs.ReviewFigures, *, excluded: int | None = None
) -> dict[str, str]:
    """The review figures of a row; ``excluded`` only on a totals row."""
    per_review = figures.units_per_review
    return {
        "reviews": str(figures.count),
        "units_per_review": NO_AVERAGE if per_review is None else str(per_review),
        "students_money_minor": _money_cell(figures),
        "gift_units": str(figures.gift_units),
        "excluded_test_reviews": "" if excluded is None else str(excluded),
    }


def _action_csv_fields(
    processing: billing_costs.ActionSpend,
    prep: billing_costs.ActionSpend,
    review: billing_costs.ActionSpend,
    other: billing_costs.ActionSpend,
) -> dict[str, str]:
    return {
        "processing_calls": str(processing.calls),
        "processing_usd": _format_decimal_usd(processing.known_usd),
        "task_preparation_calls": str(prep.calls),
        "task_preparation_usd": _format_decimal_usd(prep.known_usd),
        "review_calls": str(review.calls),
        "review_usd": _format_decimal_usd(review.known_usd),
        "other_calls": str(other.calls),
        "other_usd": _format_decimal_usd(other.known_usd),
    }


def _render_cost_csv(report: billing_costs.CostReport) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDNAMES, lineterminator="\n")
    writer.writeheader()

    if report.by in ("author", "both"):
        for row in report.authors:
            writer.writerow(
                {
                    "section": "author",
                    "row_kind": "row",
                    "tenant_id": str(row.tenant_id) if row.tenant_id else "",
                    "tenant_name": row.tenant_name or "",
                    "student_id": "",
                    "submissions": "",
                    **_spend_csv_fields(row.spend),
                    **_action_csv_fields(
                        row.processing, row.task_preparation, row.review, row.other
                    ),
                    **_units_csv_fields(row.units),
                    **_reviews_csv_fields(row.reviews),
                    **_NO_TASK_CSV,
                    "note": "",
                }
            )
        totals = report.authors_total
        writer.writerow(
            {
                "section": "author",
                "row_kind": "total",
                "tenant_id": "",
                "tenant_name": "разом",
                "student_id": "",
                "submissions": "",
                **_spend_csv_fields(totals.spend),
                **_action_csv_fields(
                    totals.processing,
                    totals.task_preparation,
                    totals.review,
                    totals.other,
                ),
                **_units_csv_fields(totals.units),
                **_reviews_csv_fields(
                    totals.reviews, excluded=report.excluded_test_reviews
                ),
                **_NO_TASK_CSV,
                "note": f"вартість невідома: {totals.spend.unknown_calls} викликів",
            }
        )

    if report.by in ("student", "both"):
        for srow in report.students:
            writer.writerow(
                {
                    "section": "student",
                    "row_kind": "row",
                    "tenant_id": str(srow.tenant_id),
                    "tenant_name": srow.tenant_name or "",
                    "student_id": str(srow.student_id),
                    "submissions": str(srow.submissions),
                    **_spend_csv_fields(srow.spend),
                    **_EMPTY_ACTION_CSV,
                    **_units_csv_fields(srow.units),
                    **_NO_REVIEWS_CSV,
                    **_NO_TASK_CSV,
                    "note": "",
                }
            )
        stotals = report.students_total
        writer.writerow(
            {
                "section": "student",
                "row_kind": "total",
                "tenant_id": "",
                "tenant_name": "разом",
                "student_id": "",
                "submissions": str(stotals.submissions),
                **_spend_csv_fields(stotals.spend),
                **_EMPTY_ACTION_CSV,
                **_units_csv_fields(stotals.units),
                **_NO_REVIEWS_CSV,
                "excluded_test_reviews": str(report.excluded_test_reviews),
                **_NO_TASK_CSV,
                "note": f"вартість невідома: {stotals.spend.unknown_calls} викликів",
            }
        )

    if report.by == "task":
        for trow in report.tasks:
            writer.writerow(
                {
                    "section": "task",
                    "row_kind": "row",
                    "tenant_id": str(trow.tenant_id) if trow.tenant_id else "",
                    "tenant_name": trow.tenant_name or "",
                    "student_id": "",
                    "submissions": "",
                    **_spend_csv_fields(trow.spend),
                    **_EMPTY_ACTION_CSV,
                    **_units_csv_fields(trow.units),
                    **_reviews_csv_fields(trow.reviews),
                    "course_id": str(trow.course_node_id)
                    if trow.course_node_id
                    else "",
                    "course_title": trow.course_title or "",
                    "task_id": str(trow.authored_document_id)
                    if trow.authored_document_id
                    else "",
                    "task_title": trow.task_title or "",
                    "note": "",
                }
            )
        ttotals = report.tasks_total
        writer.writerow(
            {
                "section": "task",
                "row_kind": "total",
                "tenant_id": "",
                "tenant_name": "разом",
                "student_id": "",
                "submissions": "",
                **_spend_csv_fields(ttotals.spend),
                **_EMPTY_ACTION_CSV,
                **_units_csv_fields(ttotals.units),
                **_reviews_csv_fields(
                    ttotals.reviews, excluded=report.excluded_test_reviews
                ),
                **_NO_TASK_CSV,
                "note": f"вартість невідома: {ttotals.spend.unknown_calls} викликів",
            }
        )

    return buf.getvalue().rstrip("\n")


async def costs_cmd(session: AsyncSession, args: argparse.Namespace) -> str:
    """Report real USD external service spend, with the units billed to payers
    and what the students paid for the reviews: per author and per student, or
    per author → course → task (``--by task``)."""
    # Enforce read-only at Postgres level before any query in this transaction.
    try:
        bind = session.get_bind()
    except (AttributeError, SQLAlchemyError):
        bind = None
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(text("SET TRANSACTION READ ONLY"))

    default_from, default_to = billing_costs.default_month_bounds()
    from_date = args.from_date or default_from
    to_date = args.to_date or default_to
    if from_date > to_date:
        raise BillingInvalidError(
            f"--from ({from_date.isoformat()}) must be <= --to ({to_date.isoformat()})"
        )

    tenant_id: uuid.UUID | None = None
    tenant_label: str | None = None
    if args.tenant is not None:
        tenant_id = await resolve_tenant_id(session, args.tenant)
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise BillingNotFoundError(f"tenant {args.tenant!r} not found")
        tenant_label = f"{tenant.name} ({tenant.id})"

    report = await billing_costs.build_cost_report(
        session,
        from_date=from_date,
        to_date=to_date,
        tenant_id=tenant_id,
        by=args.by,
        include_test_students=args.include_test_students,
    )
    if args.fmt == "csv":
        return _render_cost_csv(report)
    return _render_cost_table(report, tenant_label=tenant_label)


def creem_lookup() -> TransactionLookup:
    """Creem's adapter with the application's keys (a test replaces it)."""
    settings = get_settings()
    try:
        provider = adapters(settings.environment)[CREEM](settings)
    except PaymentConfigError as exc:
        raise BillingInvalidError(f"creem-sync needs Creem's keys: {exc}") from exc
    assert isinstance(provider, CreemProvider)
    return provider


async def creem_sync(session: AsyncSession, args: argparse.Namespace) -> str:
    """Ask Creem's API for the tax, refunded part and fee of the period's purchases."""
    if args.dry_run:
        await session.execute(text("SET TRANSACTION READ ONLY"))
    done = await provider_figures.sync_batches(
        session,
        creem_lookup(),
        from_date=args.from_date,
        to_date=args.to_date,
        actor=args.actor,
        dry_run=args.dry_run,
        now=datetime.now(UTC),
    )
    lines = [
        f"creem batches {args.from_date.isoformat()}..{args.to_date.isoformat()}: "
        f"{done.batches}  {'would update' if done.dry_run else 'updated'}="
        f"{done.updated}  unchanged={done.unchanged}  not_found={done.not_found}  "
        f"other_currency={done.other_currency}  failed={done.failed}",
        "fee: Creem's API does not give it (provider_fee_minor stays empty)",
    ]
    if done.failed:
        lines.append("failed batches were left as they were: run it again")
    if done.dry_run:
        lines.append("dry run: nothing written")
    return "\n".join(lines)


def _money_notes(line: partner_report.MoneyLine) -> list[str]:
    notes = [partner_report.fee_note(p) for p in line.fee_unknown]
    notes += [partner_report.no_data_note(p, n) for p, n in line.without_provider_data]
    return notes


def _money_text(line: partner_report.MoneyLine, rate: int | None) -> str:
    fee = "-" if line.fee_minor is None else str(line.fee_minor)
    share = (
        partner_report.NO_RATE
        if line.partner_share_minor is None
        else str(line.partner_share_minor)
    )
    text_ = (
        f"{line.currency}: gross_minor={line.gross_minor}  "
        f"tax_minor={line.tax_minor}  fee_minor={fee}  net_minor={line.net_minor}  "
        f"share_rate={_rate(rate)}  partner_share_minor={share}"
    )
    notes = _money_notes(line)
    return text_ + (f"  ({'; '.join(notes)})" if notes else "")


def _figures_text(
    figures: partner_report.PartnerFigures, rate: int | None, indent: str
) -> list[str]:
    lines = [
        f"{indent}reviews={figures.reviews}  gift_units={figures.gift_units}  "
        f"model_cost_usd={_fmt_usd(figures.model_cost_usd)}"
        + (
            f"  unknown_cost_calls={figures.unknown_cost_calls}"
            if figures.unknown_cost_calls
            else ""
        )
    ]
    lines += [f"{indent}  {_money_text(m, rate)}" for m in figures.money]
    return lines


def _render_partner_text(report: partner_report.PartnerReport) -> str:
    rate = report.share_bps
    lines = [
        f"partner report (internal) — {report.tenant_name} ({report.tenant_id})  "
        f"{report.from_date.isoformat()}..{report.to_date.isoformat()} UTC",
        f"share rate: {_rate(rate)}",
        "base: the students' money spent on reviews in the month "
        "(units bought and not used are not in it)",
    ]
    for course in report.courses:
        lines.append(f"course {course.title!r} ({course.course_node_id})")
        lines += _figures_text(course.figures, rate, "  ")
        for task in course.tasks:
            lines.append(f"  task {task.title!r} ({task.authored_document_id})")
            lines += _figures_text(task.figures, rate, "    ")
    if report.unlinked.reviews:
        lines.append("without a course of the school")
        lines += _figures_text(report.unlinked, rate, "  ")
    lines.append("total")
    lines += _figures_text(report.totals, rate, "  ")
    if report.refunds:
        lines += [
            f"refunds to students in the month: {r.kind} {r.currency}  "
            f"count={r.count}  units={r.units}  amount_minor={r.amount_minor}"
            for r in report.refunds
        ]
    else:
        lines.append("refunds to students in the month: none")
    if report.excluded_test_reviews:
        lines.append(
            f"service students: {report.excluded_test_reviews} review(s) left out"
        )
    return "\n".join(lines)


_PARTNER_CSV_FIELDS = (
    "level",
    "course_node_id",
    "course_title",
    "authored_document_id",
    "task_title",
    "currency",
    "reviews",
    "gift_units",
    "model_cost_usd",
    "unknown_cost_calls",
    "gross_minor",
    "tax_minor",
    "fee_minor",
    "net_minor",
    "share_rate",
    "partner_share_minor",
    "notes",
    "refund_kind",
    "refund_count",
    "refund_units",
    "refund_amount_minor",
)


def _partner_csv_rows(
    level: str,
    figures: partner_report.PartnerFigures,
    rate: int | None,
    **where: str,
) -> list[dict[str, str]]:
    """One line per currency; the row's reviews and dollars on its first only."""
    row = {
        "level": level,
        **where,
        "reviews": str(figures.reviews),
        "gift_units": str(figures.gift_units),
        "model_cost_usd": _format_decimal_usd(figures.model_cost_usd),
        "unknown_cost_calls": str(figures.unknown_cost_calls),
    }
    if not figures.money:
        return [row]
    lines: list[dict[str, str]] = []
    for index, money in enumerate(figures.money):
        line = dict(row) if index == 0 else {"level": level, **where}
        line |= {
            "currency": money.currency,
            "gross_minor": str(money.gross_minor),
            "tax_minor": str(money.tax_minor),
            "fee_minor": "" if money.fee_minor is None else str(money.fee_minor),
            "net_minor": str(money.net_minor),
            "share_rate": _rate(rate),
            "partner_share_minor": (
                ""
                if money.partner_share_minor is None
                else str(money.partner_share_minor)
            ),
            "notes": "; ".join(_money_notes(money)),
        }
        lines.append(line)
    return lines


def _render_partner_csv(report: partner_report.PartnerReport) -> str:
    rate = report.share_bps
    rows: list[dict[str, str]] = []
    for course in report.courses:
        where = {
            "course_node_id": str(course.course_node_id),
            "course_title": course.title or "",
        }
        rows += _partner_csv_rows("course", course.figures, rate, **where)
        for task in course.tasks:
            rows += _partner_csv_rows(
                "task",
                task.figures,
                rate,
                **where,
                authored_document_id=str(task.authored_document_id),
                task_title=task.title or "",
            )
    if report.unlinked.reviews:
        rows += _partner_csv_rows("unlinked", report.unlinked, rate)
    rows += _partner_csv_rows("total", report.totals, rate)
    rows += [
        {
            "level": "refund",
            "currency": r.currency,
            "refund_kind": r.kind,
            "refund_count": str(r.count),
            "refund_units": str(r.units),
            "refund_amount_minor": str(r.amount_minor),
        }
        for r in report.refunds
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=_PARTNER_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().rstrip("\n")


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID | date):
        return str(value)
    if isinstance(value, Decimal):
        return _format_decimal_usd(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def _render_partner_json(report: partner_report.PartnerReport) -> str:
    found = dataclasses.asdict(report)
    found["share_rate"] = None if report.share_bps is None else _rate(report.share_bps)
    found["internal"] = True
    return json.dumps(found, ensure_ascii=False, indent=2, default=_json_value)


async def partner_report_cmd(session: AsyncSession, args: argparse.Namespace) -> str:
    """A school's month: its students' money for reviews, tax, fee, cost, share."""
    await session.execute(text("SET TRANSACTION READ ONLY"))
    tenant_id = await resolve_tenant_id(session, args.tenant)
    report = await partner_report.partner_report(
        session, tenant_id=tenant_id, month=args.month
    )
    if args.fmt == "csv":
        return _render_partner_csv(report)
    if args.fmt == "json":
        return _render_partner_json(report)
    return _render_partner_text(report)


COMMANDS: dict[str, Handler] = {
    "show-account": show_account,
    "get-settings": get_settings_cmd,
    "set-benefit": set_benefit,
    "set-credit": set_credit,
    "list-rules": list_rules,
    "create-rule": create_rule,
    "end-rule": end_rule,
    "anonymize-student": anonymize_student,
    "anonymize-tenant": anonymize_tenant,
    "credit": credit,
    "credit-invoice": credit_invoice,
    "refund-hold": refund_hold,
    "refund-release": refund_release,
    "test-purchase": mark_test_purchase,
    "void-test-batches": void_test_batches,
    "test-student": test_student,
    "dispute-lost": dispute_lost,
    "dispute-won": dispute_won,
    "balance": balance,
    "statement": statement,
    "costs": costs_cmd,
    "set-partner-share": set_partner_share,
    "creem-sync": creem_sync,
    "partner-report": partner_report_cmd,
}

# Commands that open their own transactions rather than run in the one of run().
SELF_TRANSACTED: dict[str, Callable[[argparse.Namespace], Awaitable[str]]] = {
    "run-periodic": run_periodic,
}


_COSTS_FIELDS_HELP = """\
report real USD external service spend, with the units billed and what the
students paid for the reviews. The review fields (author and task rows):

  reviews               different reviews with a debit on a student's own
                        account in the period.
  units_per_review      the row's billed students' units divided by reviews,
                        rounded up; a dash when there are no reviews.
  students_money_minor  money the students paid for those reviews, per
                        currency in minor units: each debit from a paid
                        purchase batch is units * amount_paid_minor / batch
                        units, summed per row and rounded once, half up.
  gift_units            units debited from batches without money (an admin
                        credit, or a purchase marked as a test one).

Refunds (billing_refunds) are not subtracted from students_money_minor and are
not counted in gift_units.
Test students (students.is_test_student) are left out of these fields and of
the students' units unless --include-test-students.
"""


_PARTNER_FIELDS_HELP = """\
a school's month (UTC, both edges inclusive) of reviews in money — INTERNAL:
what of it the school is shown is the product owner's decision. By course ->
task and in total, each currency apart:

  reviews              reviews the school's students paid from their own
                       accounts (service students left out).
  gross_minor          their money spent on those reviews, tax included: a
                       debit from a paid batch is units * amount_paid_minor /
                       batch units. Units bought and not used are not in it.
  tax_minor            each batch's tax for the units spent from it: units /
                       batch units * provider_tax_minor. A batch whose tax
                       Creem has not told yet adds none: "без даних Creem"
                       (creem-sync asks it).
  fee_minor            the same of the provider's fee where known; Creem's API
                       does not give it: "комісія Creem: API не віддає".
  net_minor            gross_minor - tax_minor - fee_minor (the fee where
                       known).
  model_cost_usd       what the models cost for these reviews, USD.
  share_rate           the school's rate (set-partner-share).
  partner_share_minor  share_rate * net_minor; "ставку не задано" without one.
  refunds              money given back to the school's students in the
                       month (refunds approved, disputes lost), apart.

In csv a row of two currencies has two lines; its reviews, gift_units and
dollars are on the first only.
"""


def build_parser() -> argparse.ArgumentParser:
    """The command line (module docstring)."""
    parser = argparse.ArgumentParser(description="Billing administration CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_: str, *, changes: bool) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        if changes:
            p.add_argument("--actor", required=True, help="who makes the change")
        return p

    p = command("show-account", "show an account", changes=False)
    p.add_argument("--account", type=uuid.UUID)
    p.add_argument("--tenant", help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID)

    p = command("get-settings", "show author settings", changes=False)
    p.add_argument("--tenant", required=True, help="tenant id or name")

    p = command("set-benefit", "set the author's benefit", changes=True)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--benefit", required=True, choices=[b.value for b in Benefit])
    p.add_argument("--limit", type=int, help="units a month (monthly_limit)")

    p = command("set-credit", "allow or forbid credit", changes=True)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--allowed", required=True, type=_yes_no, help="yes | no")

    p = command("list-rules", "list payment rules", changes=False)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--all", action="store_true", help="include ended rules")

    p = command("create-rule", "set a payment rule", changes=True)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--scope", required=True, choices=[s.value for s in RuleScope])
    p.add_argument("--action", required=True, choices=[k.value for k in ActionKind])
    p.add_argument("--course", type=uuid.UUID, help="root node id (scope course)")
    p.add_argument("--student", type=uuid.UUID, help="student id (scope student)")
    p.add_argument("--payer", type=uuid.UUID, help="author account id (default)")

    p = command("end-rule", "end a payment rule", changes=True)
    p.add_argument("--rule", required=True, type=uuid.UUID)

    p = command("anonymize-student", "after a student's deletion", changes=True)
    p.add_argument("--student", required=True, type=uuid.UUID)

    p = command("anonymize-tenant", "after a tenant's deletion", changes=True)
    p.add_argument("--tenant", required=True, type=uuid.UUID, help="tenant id")

    p = command("credit", "credit units (a batch of its own)", changes=True)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID, help="student id (else the author)")
    p.add_argument("--units", required=True, type=int)
    p.add_argument("--reason", required=True, help="why the units are granted")

    p = command(
        "test-purchase", "may the account buy in the provider's test mode", changes=True
    )
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID, help="student id (else the author)")
    mark = p.add_mutually_exclusive_group(required=True)
    mark.add_argument(
        "--allow", dest="allowed", action="store_true", help="mark the account"
    )
    mark.add_argument(
        "--deny", dest="allowed", action="store_false", help="clear the mark"
    )

    p = command(
        "void-test-batches",
        "take the free units of test purchases out",
        changes=True,
    )
    p.add_argument("--account", type=uuid.UUID, help="one account only")
    p.add_argument(
        "--dry-run", action="store_true", help="print what would go; write nothing"
    )

    p = command("test-student", "mark a service student, or list them", changes=False)
    p.add_argument("--student", type=uuid.UUID, help="student id (--mark, --unmark)")
    p.add_argument("--actor", help="who makes the change (--mark, --unmark)")
    mark = p.add_mutually_exclusive_group(required=True)
    mark.add_argument(
        "--mark", dest="mark", action="store_const", const=True, help="set the mark"
    )
    mark.add_argument(
        "--unmark", dest="mark", action="store_const", const=False, help="clear it"
    )
    mark.add_argument("--list", action="store_true", help="list the marked students")

    p = command("credit-invoice", "credit a paid contract invoice", changes=True)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--units", required=True, type=int)
    p.add_argument(
        "--amount-minor",
        dest="amount_minor",
        required=True,
        type=int,
        help="the amount paid, in minor units (cents)",
    )
    p.add_argument("--currency", required=True, help="ISO 4217 code, e.g. EUR")
    p.add_argument("--invoice", required=True, help="the invoice's number")

    p = command("refund-hold", "hold an order's units before its refund", changes=True)
    p.add_argument("--order", required=True, help="the provider's order id")
    p.add_argument("--units", required=True, type=int)
    p.add_argument("--provider", default="creem", help="the provider (creem)")
    p.add_argument("--reason", required=True, help="why the user is refunded")

    p = command("refund-release", "the refund did not happen", changes=True)
    p.add_argument("--order", required=True, help="the provider's order id")
    p.add_argument("--provider", default="creem", help="the provider (creem)")

    for name, help_ in (
        ("dispute-lost", "a dispute lost: its held units leave the batch"),
        ("dispute-won", "a dispute won: its held units are free again"),
    ):
        p = command(name, help_, changes=True)
        p.add_argument("--dispute", required=True, help="the provider's dispute id")
        p.add_argument("--provider", help="the provider (when two share the id)")

    p = command("balance", "show the balance of an account", changes=False)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID, help="student id (else the author)")

    p = command("statement", "list the movements of an account", changes=False)
    p.add_argument("--account", required=True, type=uuid.UUID)
    p.add_argument("--from", dest="since", type=_instant, help="from (inclusive)")
    p.add_argument("--to", dest="until", type=_instant, help="to (exclusive)")

    p = command(
        "costs",
        "report real USD external service spend, with the units billed",
        changes=False,
    )
    p.add_argument(
        "--from",
        dest="from_date",
        type=_utc_date,
        help="start UTC date YYYY-MM-DD, inclusive (default: 1st of UTC month)",
    )
    p.add_argument(
        "--to",
        dest="to_date",
        type=_utc_date,
        help="end UTC date YYYY-MM-DD, inclusive (default: today UTC)",
    )
    p.add_argument("--tenant", help="tenant id or name (default: all)")
    p.add_argument(
        "--by",
        choices=("author", "student", "both", "task"),
        default="both",
        help="grouping scope (default: both); task: author -> course -> task",
    )
    p.add_argument(
        "--format",
        dest="fmt",
        choices=("table", "csv"),
        default="table",
        help="output format (default: table)",
    )
    p.add_argument(
        "--include-test-students",
        dest="include_test_students",
        action="store_true",
        help="count the reviews and units of test students too "
        "(default: left out and counted in the 'excluded' line)",
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.description = _COSTS_FIELDS_HELP

    p = command(
        "set-partner-share",
        "set the school's share rate (partner report)",
        changes=True,
    )
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument(
        "--percent",
        required=True,
        type=_percent,
        help="0 to 100, at most two decimals (12.5), or none",
    )

    p = command(
        "creem-sync",
        "write Creem's tax and refunded part on the period's purchases",
        changes=True,
    )
    p.add_argument(
        "--from",
        dest="from_date",
        required=True,
        type=_utc_date,
        help="batches created from this UTC date YYYY-MM-DD, inclusive",
    )
    p.add_argument(
        "--to",
        dest="to_date",
        required=True,
        type=_utc_date,
        help="to this UTC date YYYY-MM-DD, inclusive",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="ask and count only; write nothing"
    )

    p = command(
        "partner-report",
        "a school's month of reviews in money (internal)",
        changes=False,
    )
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--month", required=True, type=_month, help="YYYY-MM, UTC")
    p.add_argument(
        "--format",
        dest="fmt",
        choices=("text", "csv", "json"),
        default="text",
        help="output format (default: text)",
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.description = _PARTNER_FIELDS_HELP

    p = command("run-periodic", "run a periodic task by hand", changes=False)
    p.add_argument(
        "--task",
        required=True,
        choices=[t.name for t in BILLING_TASKS],
        help="; ".join(f"{t.name}: {t.summary}" for t in BILLING_TASKS),
    )
    p.add_argument(
        "--dry-run", action="store_true", help="count only; roll everything back"
    )
    return parser


async def run(args: argparse.Namespace) -> str:
    """Run one command in one transaction: committed on success only.

    The exceptions (``SELF_TRANSACTED``) own their transactions: a periodic
    task opens its own.
    """
    if args.command in SELF_TRANSACTED:
        return await SELF_TRANSACTED[args.command](args)
    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            return await COMMANDS[args.command](session, args)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the command, print its result."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "test-student" and (
        not args.list and (args.student is None or args.actor is None)
    ):
        parser.error("test-student --mark/--unmark needs --student and --actor")
    try:
        out = asyncio.run(run(args))
    except BillingError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1
    except PeriodicRunError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
