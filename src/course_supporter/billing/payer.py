"""Who pays for an action — the ONE place in the code that decides it (BIL-A).

Purpose:
    Given an action — its kind, tenant, course and student — return the
    account that pays and the account the action is for. Charging is not here
    (BIL-E); neither is any call from a review or a processing job yet: this
    function is what BIL-E will call.

The order, first match wins:

    1. an active rule for the STUDENT and this kind of action;
    2. an active rule for the COURSE and this kind of action;
    3. the default, "self": a review is paid by the student's account, a
       processing or a task preparation by the author's (the tenant's).

The account "for whom" is always the default payer's: the student's for a
review, the author's otherwise. A rule whose payer account is no longer active
(its tenant was deleted) is not applied.

Two sides, one order (BIL-E3):

* :func:`resolve_payer` — for an action that changes state: the default
  payer's account is created on first need (:mod:`.accounts`, audited), so
  the caller's transaction may gain a row;
* :func:`find_payer` — for a read (an estimate, a reminder, a list): creates
  nothing; an account that does not exist yet is ``None`` and reads as an
  empty one. A rule's payer always exists (a rule names an active account).

Input:
    ``course_node_id`` is the course — the ROOT ``CourseNode`` — as the caller
    knows it; it is not looked up. ``student_id`` is required for a review and
    optional otherwise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.accounts import (
    find_active_account,
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import (
    AccountKind,
    AccountStatus,
    ActionKind,
    PayerSource,
    RuleScope,
)
from course_supporter.storage.orm import BillingAccount, BillingPaymentRule


@dataclass(frozen=True)
class PayerResolution:
    """Who pays for an action, for whom, and why."""

    payer_account_id: uuid.UUID
    beneficiary_account_id: uuid.UUID
    """The account the action is for ("за кого")."""
    source: PayerSource
    rule_id: uuid.UUID | None
    """The rule applied; None for the default."""


@dataclass(frozen=True)
class PayerLookup:
    """Who would pay for an action, read without creating an account.

    ``None`` stands for an account that does not exist yet: the default
    payer's own, before the first action that changes state. It reads as an
    empty account.
    """

    payer_account_id: uuid.UUID | None
    beneficiary_account_id: uuid.UUID | None
    """The account the action is for ("за кого"); None before it exists."""
    source: PayerSource
    rule_id: uuid.UUID | None
    """The rule applied; None for the default."""


def default_kind(action_kind: ActionKind) -> AccountKind:
    """The kind of the default payer's account: a student's for a review."""
    return (
        AccountKind.STUDENT if action_kind is ActionKind.REVIEW else AccountKind.AUTHOR
    )


async def _active_rule(
    session: AsyncSession,
    *,
    scope: RuleScope,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
) -> BillingPaymentRule | None:
    target = (
        BillingPaymentRule.student_id
        if scope is RuleScope.STUDENT
        else BillingPaymentRule.course_node_id
    )
    stmt = (
        select(BillingPaymentRule)
        .join(BillingAccount, BillingAccount.id == BillingPaymentRule.payer_account_id)
        .where(
            BillingPaymentRule.active.is_(True),
            BillingPaymentRule.scope == scope.value,
            BillingPaymentRule.action_kind == action_kind.value,
            BillingPaymentRule.tenant_id == tenant_id,
            target == target_id,
            BillingAccount.status == AccountStatus.ACTIVE.value,
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _student_of_review(student_id: uuid.UUID | None) -> uuid.UUID:
    if student_id is None:
        raise BillingInvalidError("a review is always for a student")
    return student_id


async def _applied_rule(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> tuple[BillingPaymentRule, PayerSource] | None:
    """The first active rule of the order (module docstring), or None."""
    candidates = (
        (RuleScope.STUDENT, student_id, PayerSource.STUDENT_RULE),
        (RuleScope.COURSE, course_node_id, PayerSource.COURSE_RULE),
    )
    for scope, target_id, source in candidates:
        if target_id is None:
            continue
        rule = await _active_rule(
            session,
            scope=scope,
            action_kind=action_kind,
            tenant_id=tenant_id,
            target_id=target_id,
        )
        if rule is not None:
            return rule, source
    return None


async def resolve_payer(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> PayerResolution:
    """Return the payer and the beneficiary of one action (module docstring).

    For an action that changes state: the beneficiary's account is created on
    first need (by :data:`.audit.SYSTEM_ACTOR`). A read uses
    :func:`find_payer`.

    Raises:
        BillingInvalidError: a review without a student.
    """
    if action_kind is ActionKind.REVIEW:
        beneficiary = await get_or_create_student_account(
            session, tenant_id=tenant_id, student_id=_student_of_review(student_id)
        )
    else:
        beneficiary = await get_or_create_author_account(session, tenant_id=tenant_id)
    applied = await _applied_rule(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    if applied is not None:
        rule, source = applied
        return PayerResolution(
            payer_account_id=rule.payer_account_id,
            beneficiary_account_id=beneficiary.id,
            source=source,
            rule_id=rule.id,
        )
    return PayerResolution(
        payer_account_id=beneficiary.id,
        beneficiary_account_id=beneficiary.id,
        source=PayerSource.DEFAULT,
        rule_id=None,
    )


async def find_payer(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> PayerLookup:
    """:func:`resolve_payer` for a read: the same order, nothing created.

    Raises:
        BillingInvalidError: a review without a student.
    """
    found = await find_active_account(
        session,
        kind=default_kind(action_kind),
        tenant_id=tenant_id,
        student_id=_student_of_review(student_id)
        if action_kind is ActionKind.REVIEW
        else None,
    )
    beneficiary_id = found.id if found is not None else None
    applied = await _applied_rule(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    if applied is not None:
        rule, source = applied
        return PayerLookup(
            payer_account_id=rule.payer_account_id,
            beneficiary_account_id=beneficiary_id,
            source=source,
            rule_id=rule.id,
        )
    return PayerLookup(
        payer_account_id=beneficiary_id,
        beneficiary_account_id=beneficiary_id,
        source=PayerSource.DEFAULT,
        rule_id=None,
    )
