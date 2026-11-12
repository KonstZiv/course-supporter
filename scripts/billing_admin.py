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

Anonymization, AFTER the tenant or the student was deleted by hand (there is
no account-deletion flow yet; when one appears it calls the same functions).
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

Exit status: 0 on success; 1 when the operation is refused (the message says
why; nothing is written) or a periodic task failed or found its lock held;
2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from course_supporter.billing import accounts, admin, ledger
from course_supporter.billing import costs as billing_costs
from course_supporter.billing.errors import (
    BillingError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.kinds import ActionKind, Benefit, RuleScope
from course_supporter.billing.payments import invoice
from course_supporter.billing.periodic import BILLING_TASKS
from course_supporter.config import get_settings
from course_supporter.periodic.runner import TaskRun, run_task
from course_supporter.storage.orm import (
    BillingAccount,
    BillingMovement,
    BillingPaymentRule,
    Tenant,
)

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


# ── Output ─────────────────────────────────────────────────────────────


def _account_line(account: BillingAccount) -> str:
    return (
        f"account {account.id}  kind={account.kind}  status={account.status}  "
        f"tenant={account.tenant_id}  student={account.student_id}  "
        f"created={account.created_at:%Y-%m-%d}  closed={account.closed_at}  "
        f"anonymized={account.anonymized_at}"
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
    return (
        f"author account {view.account_id}  benefit={view.benefit.value}{limit}  "
        f"since={view.benefit_started_at}  credit_allowed={view.credit_allowed}"
    )


def _balance_line(account_id: uuid.UUID, view: ledger.Balance) -> str:
    expiry = f"{view.next_expiry:%Y-%m-%d}" if view.next_expiry else "-"
    return (
        f"account {account_id}  balance={view.balance}  reserved={view.reserved}  "
        f"held={view.held}  available={view.available}  "
        f"difference={view.difference}  next_expiry={expiry}"
    )


def _movement_line(m: BillingMovement) -> str:
    action = f"{m.action_kind}:{m.action_key}" if m.action_kind else "-"
    return (
        f"{m.created_at:%Y-%m-%d %H:%M:%S}  {m.kind:<19} {m.units:>7}  "
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
        session, tenant_id=tenant_id, student_id=args.student, create=True
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
    ) -> list[str]:
        cells = [
            tenant_id_str,
            tenant_name_str,
            str(spend.calls),
            _fmt_usd(spend.known_usd),
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
    )
    lines = ["По авторах:"]
    lines.extend(_format_ascii_table(headers, rows, total_row, right_align={2, 3, 6}))
    lines.append(f"вартість невідома: {totals.spend.unknown_calls} викликів")
    return lines


def _render_student_table(report: billing_costs.CostReport) -> list[str]:
    headers = [
        "орендар_id",
        "орендар",
        "студент_id",
        "подачі",
        "виклики",
        "сума_usd",
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
    ) -> list[str]:
        return [
            tenant_id_str,
            tenant_name_str,
            student_id_str,
            str(submissions),
            str(spend.calls),
            _fmt_usd(spend.known_usd),
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
        )
        for row in report.students
    ]
    totals = report.students_total
    total_row = _cells("разом", "", "", totals.submissions, totals.spend)
    lines = ["По студентах:"]
    lines.extend(
        _format_ascii_table(headers, rows, total_row, right_align={3, 4, 5, 8})
    )
    lines.append(f"вартість невідома: {totals.spend.unknown_calls} викликів")
    return lines


def _render_cost_table(
    report: billing_costs.CostReport, *, tenant_label: str | None
) -> str:
    tenant_text = tenant_label or "усі"
    lines = [
        (
            f"Період (UTC): {report.from_date.isoformat()} .. "
            f"{report.to_date.isoformat()} (включно) | Орендар: {tenant_text}"
        )
    ]
    if report.by in ("author", "both"):
        lines.append("")
        lines.extend(_render_author_table(report))
    if report.by in ("student", "both"):
        lines.append("")
        lines.extend(_render_student_table(report))
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
    "note",
)

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
        "exact_calls": str(spend.exact_calls),
        "exact_usd": _format_decimal_usd(spend.exact_usd),
        "estimated_calls": str(spend.estimated_calls),
        "estimated_usd": _format_decimal_usd(spend.estimated_usd),
        "unknown_calls": str(spend.unknown_calls),
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
                "note": f"вартість невідома: {stotals.spend.unknown_calls} викликів",
            }
        )

    return buf.getvalue().rstrip("\n")


async def costs_cmd(session: AsyncSession, args: argparse.Namespace) -> str:
    """Report real USD external service spend per author and per student."""
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
    )
    if args.fmt == "csv":
        return _render_cost_csv(report)
    return _render_cost_table(report, tenant_label=tenant_label)


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
    "balance": balance,
    "statement": statement,
    "costs": costs_cmd,
}

# Commands that open their own transactions rather than run in the one of run().
SELF_TRANSACTED: dict[str, Callable[[argparse.Namespace], Awaitable[str]]] = {
    "run-periodic": run_periodic,
}


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

    p = command("balance", "show the balance of an account", changes=False)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID, help="student id (else the author)")

    p = command("statement", "list the movements of an account", changes=False)
    p.add_argument("--account", required=True, type=uuid.UUID)
    p.add_argument("--from", dest="since", type=_instant, help="from (inclusive)")
    p.add_argument("--to", dest="until", type=_instant, help="to (exclusive)")

    p = command("costs", "report real USD external service spend", changes=False)
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
        choices=("author", "student", "both"),
        default="both",
        help="grouping scope (default: both)",
    )
    p.add_argument(
        "--format",
        dest="fmt",
        choices=("table", "csv"),
        default="table",
        help="output format (default: table)",
    )

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
    args = build_parser().parse_args(argv)
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
