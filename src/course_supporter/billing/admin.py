"""The platform administrator's billing operations (BIL-A).

Purpose:
    One service layer for every administrative read and change of billing
    data: read an account, the settings of an author account, the payment
    rules. Today the only entry point is ``scripts/billing_admin.py``, run
    inside the application container (operator decision 2026-11-05: no
    administrator routes until there is a decision on how an administrator
    signs in). The routes, when they come, call these same functions.

Audit:
    Every change writes one audit entry (:mod:`.audit`) in the caller's
    transaction, with the ``actor`` the caller names. A call that changes
    nothing writes nothing.

Rules:
    A payment rule moves an action's payment from its default payer to the
    AUTHOR account of the same tenant (e.g. "reviews on course X are paid by
    the author"). A student never pays for another student: a rule whose payer
    is not that author account is refused. One active rule per (target, kind
    of action); to change a rule, end it and set a new one.

Author settings:
    Benefit (none | monthly_limit | unlimited) and credit. A change of the KIND
    of benefit stamps ``benefit_started_at``; a new limit within monthly_limit
    keeps it. Computing what a limit leaves is BIL-B / BIL-E.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.billing.errors import (
    BillingConflictError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.kinds import (
    AccountKind,
    AccountStatus,
    ActionKind,
    AuditAction,
    Benefit,
    RuleScope,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuthorSettings,
    BillingConsent,
    BillingPaymentRule,
    CourseNode,
    Student,
    Tenant,
)

# ── Views ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorSettingsView:
    """The settings of an author account; the defaults when none were set."""

    account_id: uuid.UUID
    benefit: Benefit
    monthly_limit_units: int | None
    benefit_started_at: datetime | None
    credit_allowed: bool
    updated_at: datetime | None
    """None while the defaults were never changed."""


@dataclass(frozen=True)
class AccountView:
    """One account with what hangs on it."""

    account: BillingAccount
    settings: AuthorSettingsView | None
    """Author accounts only."""
    rules_paid: list[BillingPaymentRule]
    """Active rules this account pays under."""
    consents: list[BillingConsent]


def _settings_view(
    account_id: uuid.UUID, row: BillingAuthorSettings | None
) -> AuthorSettingsView:
    if row is None:
        return AuthorSettingsView(
            account_id=account_id,
            benefit=Benefit.NONE,
            monthly_limit_units=None,
            benefit_started_at=None,
            credit_allowed=False,
            updated_at=None,
        )
    return AuthorSettingsView(
        account_id=account_id,
        benefit=Benefit(row.benefit),
        monthly_limit_units=row.monthly_limit_units,
        benefit_started_at=row.benefit_started_at,
        credit_allowed=row.credit_allowed,
        updated_at=row.updated_at,
    )


# ── Lookups ────────────────────────────────────────────────────────────


async def _live_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        raise BillingNotFoundError(f"tenant {tenant_id} not found")
    return tenant


async def _active_rules_paid_by(
    session: AsyncSession, account_id: uuid.UUID
) -> list[BillingPaymentRule]:
    rows = await session.execute(
        select(BillingPaymentRule)
        .where(
            BillingPaymentRule.payer_account_id == account_id,
            BillingPaymentRule.active.is_(True),
        )
        .order_by(BillingPaymentRule.created_at)
    )
    return list(rows.scalars())


# ── Accounts (read) ────────────────────────────────────────────────────


async def read_account(session: AsyncSession, account_id: uuid.UUID) -> AccountView:
    """The account, its author settings, the rules it pays under, its consents."""
    account = await session.get(BillingAccount, account_id)
    if account is None:
        raise BillingNotFoundError(f"account {account_id} not found")
    settings = None
    if account.kind == AccountKind.AUTHOR.value:
        row = await session.get(BillingAuthorSettings, account.id)
        settings = _settings_view(account.id, row)
    consents = await session.execute(
        select(BillingConsent)
        .where(BillingConsent.account_id == account.id)
        .order_by(BillingConsent.consented_at)
    )
    return AccountView(
        account=account,
        settings=settings,
        rules_paid=await _active_rules_paid_by(session, account.id),
        consents=list(consents.scalars()),
    )


async def find_accounts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID | None = None,
) -> list[BillingAccount]:
    """The owner's accounts, active or closed, oldest first.

    With ``student_id`` — the student's accounts in the tenant; without it —
    the author's account(s) of the tenant. Anonymized accounts have no owner
    and are found by id only.
    """
    stmt = select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
    if student_id is None:
        stmt = stmt.where(BillingAccount.kind == AccountKind.AUTHOR.value)
    else:
        stmt = stmt.where(
            BillingAccount.kind == AccountKind.STUDENT.value,
            BillingAccount.student_id == student_id,
        )
    rows = await session.execute(stmt.order_by(BillingAccount.created_at))
    return list(rows.scalars())


# ── Author settings ────────────────────────────────────────────────────


async def get_author_settings(
    session: AsyncSession, tenant_id: uuid.UUID
) -> AuthorSettingsView:
    """The settings of the tenant's author account (created on first need)."""
    await _live_tenant(session, tenant_id)
    account = await get_or_create_author_account(session, tenant_id=tenant_id)
    row = await session.get(BillingAuthorSettings, account.id)
    return _settings_view(account.id, row)


def _next_limit(
    benefit: Benefit, current: BillingAuthorSettings, requested: int | None
) -> int | None:
    if benefit is not Benefit.MONTHLY_LIMIT:
        if requested is not None:
            raise BillingInvalidError(
                "monthly_limit_units goes with the monthly_limit benefit only"
            )
        return None
    limit = requested if requested is not None else current.monthly_limit_units
    if limit is None or limit <= 0:
        raise BillingInvalidError(
            "the monthly_limit benefit needs monthly_limit_units > 0"
        )
    return limit


async def update_author_settings(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    benefit: Benefit | None = None,
    monthly_limit_units: int | None = None,
    credit_allowed: bool | None = None,
    now: datetime | None = None,
) -> AuthorSettingsView:
    """Change what is given; leave the rest. Audited; a no-op writes nothing.

    Raises:
        BillingNotFoundError: no such live tenant.
        BillingInvalidError: a limit without the monthly_limit benefit, or
            monthly_limit without a positive limit.
    """
    actor = audit.require_actor(actor)
    await _live_tenant(session, tenant_id)
    account = await get_or_create_author_account(session, tenant_id=tenant_id)
    row = await session.get(BillingAuthorSettings, account.id)
    before = audit.settings_snapshot(row) if row is not None else None
    draft = row if row is not None else _default_settings(account.id)
    baseline = audit.settings_snapshot(draft)

    current = Benefit(draft.benefit)
    next_benefit = benefit if benefit is not None else current
    draft.monthly_limit_units = _next_limit(next_benefit, draft, monthly_limit_units)
    if next_benefit is not current:
        draft.benefit_started_at = (
            None if next_benefit is Benefit.NONE else (now or datetime.now(UTC))
        )
    draft.benefit = next_benefit.value
    if credit_allowed is not None:
        draft.credit_allowed = credit_allowed

    after = audit.settings_snapshot(draft)
    if after == baseline:
        return _settings_view(account.id, row)
    if row is None:
        session.add(draft)
    await session.flush()
    await session.refresh(draft)
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.AUTHOR_SETTINGS_UPDATE,
        entity="author_settings",
        entity_id=account.id,
        before=before,
        after=after,
    )
    return _settings_view(account.id, draft)


def _default_settings(account_id: uuid.UUID) -> BillingAuthorSettings:
    """A new, unsaved row with the defaults (no benefit, no credit)."""
    return BillingAuthorSettings(
        account_id=account_id,
        benefit=Benefit.NONE.value,
        monthly_limit_units=None,
        benefit_started_at=None,
        credit_allowed=False,
    )


# ── Payment rules ──────────────────────────────────────────────────────


async def list_payment_rules(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_ended: bool = False
) -> list[BillingPaymentRule]:
    """The tenant's rules, oldest first; active ones only unless asked."""
    stmt = select(BillingPaymentRule).where(BillingPaymentRule.tenant_id == tenant_id)
    if not include_ended:
        stmt = stmt.where(BillingPaymentRule.active.is_(True))
    rows = await session.execute(stmt.order_by(BillingPaymentRule.created_at))
    return list(rows.scalars())


async def _check_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: RuleScope,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> None:
    if scope is RuleScope.COURSE:
        if course_node_id is None or student_id is not None:
            raise BillingInvalidError("a course rule names a course and no student")
        node = await session.get(CourseNode, course_node_id)
        if (
            node is None
            or node.deleted_at is not None
            or node.tenant_id != tenant_id
            or node.parent_id is not None
        ):
            raise BillingNotFoundError(
                f"course {course_node_id} not found in tenant {tenant_id} "
                "(a course is a root node)"
            )
        return
    if student_id is None or course_node_id is not None:
        raise BillingInvalidError("a student rule names a student and no course")
    student = await session.get(Student, student_id)
    if (
        student is None
        or student.deleted_at is not None
        or (student.tenant_id != tenant_id)
    ):
        raise BillingNotFoundError(
            f"student {student_id} not found in tenant {tenant_id}"
        )


async def _rule_payer(
    session: AsyncSession, tenant_id: uuid.UUID, payer_account_id: uuid.UUID | None
) -> BillingAccount:
    if payer_account_id is None:
        return await get_or_create_author_account(session, tenant_id=tenant_id)
    payer = await session.get(BillingAccount, payer_account_id)
    if (
        payer is None
        or payer.kind != AccountKind.AUTHOR.value
        or payer.status != AccountStatus.ACTIVE.value
        or payer.tenant_id != tenant_id
    ):
        raise BillingInvalidError(
            "a rule's payer must be the active author account of the same "
            "tenant (a student never pays for another student)"
        )
    return payer


async def create_payment_rule(
    session: AsyncSession,
    *,
    actor: str,
    tenant_id: uuid.UUID,
    scope: RuleScope,
    action_kind: ActionKind,
    course_node_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    payer_account_id: uuid.UUID | None = None,
) -> BillingPaymentRule:
    """Set a rule; the payer defaults to the tenant's author account. Audited.

    Raises:
        BillingNotFoundError: no such live tenant, course (root) or student
            in the tenant.
        BillingInvalidError: the target does not match the scope, or the payer
            is not the author account of the tenant.
        BillingConflictError: an active rule for this target and kind exists.
    """
    actor = audit.require_actor(actor)
    await _live_tenant(session, tenant_id)
    await _check_target(
        session,
        tenant_id=tenant_id,
        scope=scope,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    payer = await _rule_payer(session, tenant_id, payer_account_id)
    target = (
        BillingPaymentRule.course_node_id == course_node_id
        if scope is RuleScope.COURSE
        else BillingPaymentRule.student_id == student_id
    )
    existing = await session.execute(
        select(BillingPaymentRule.id).where(
            BillingPaymentRule.active.is_(True),
            BillingPaymentRule.scope == scope.value,
            BillingPaymentRule.action_kind == action_kind.value,
            target,
        )
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        raise BillingConflictError(
            f"an active {action_kind.value} rule for this {scope.value} exists "
            f"({found}); end it first"
        )
    rule = BillingPaymentRule(
        scope=scope.value,
        action_kind=action_kind.value,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
        payer_account_id=payer.id,
        active=True,
        created_by=actor,
    )
    session.add(rule)
    await session.flush()
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.PAYMENT_RULE_CREATE,
        entity="payment_rule",
        entity_id=rule.id,
        before=None,
        after=audit.rule_snapshot(rule),
    )
    return rule


async def end_payment_rule(
    session: AsyncSession,
    rule_id: uuid.UUID,
    *,
    actor: str,
    now: datetime | None = None,
) -> BillingPaymentRule:
    """End an active rule; from then on the default (or a course rule) applies.

    Raises:
        BillingNotFoundError: no such rule.
        BillingConflictError: the rule is already ended.
    """
    actor = audit.require_actor(actor)
    rule = await session.get(BillingPaymentRule, rule_id, with_for_update=True)
    if rule is None:
        raise BillingNotFoundError(f"rule {rule_id} not found")
    if not rule.active:
        raise BillingConflictError(f"rule {rule_id} is already ended")
    before = audit.rule_snapshot(rule)
    rule.active = False
    rule.ended_at = now or datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.PAYMENT_RULE_END,
        entity="payment_rule",
        entity_id=rule.id,
        before=before,
        after=audit.rule_snapshot(rule),
    )
    return rule
