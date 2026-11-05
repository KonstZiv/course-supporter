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

Anonymization, AFTER the tenant or the student was deleted by hand (there is
no account-deletion flow yet; when one appears it calls the same functions).
It closes and anonymizes the owner's accounts and ends the active payment
rules about the owner — a student's student rules, every rule of a tenant —
each ending audited as payment_rule.end; a second run changes nothing:
    anonymize-student --student <student-id> --actor kostiantyn
    anonymize-tenant  --tenant <tenant-id> --actor kostiantyn

Exit status: 0 on success; 1 when the operation is refused (the message says
why; nothing is written); 2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from course_supporter.billing import accounts, admin, ledger
from course_supporter.billing.errors import BillingError, BillingNotFoundError
from course_supporter.billing.kinds import ActionKind, Benefit, RuleScope
from course_supporter.config import get_settings
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


def _instant(value: str) -> datetime:
    """A date or an ISO instant; without a zone it is UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
    "balance": balance,
    "statement": statement,
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

    p = command("balance", "show the balance of an account", changes=False)
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", type=uuid.UUID, help="student id (else the author)")

    p = command("statement", "list the movements of an account", changes=False)
    p.add_argument("--account", required=True, type=uuid.UUID)
    p.add_argument("--from", dest="since", type=_instant, help="from (inclusive)")
    p.add_argument("--to", dest="until", type=_instant, help="to (exclusive)")
    return parser


async def run(args: argparse.Namespace) -> str:
    """Run one command in one transaction: committed on success only."""
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
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
