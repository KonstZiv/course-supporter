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
(its tenant was deleted) is not applied. Accounts are created lazily on the
way (:mod:`.accounts`), so the caller's transaction may gain a row.

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
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import (
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


async def resolve_payer(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> PayerResolution:
    """Return the payer and the beneficiary of one action (module docstring).

    Raises:
        BillingInvalidError: a review without a student.
    """
    if action_kind is ActionKind.REVIEW:
        if student_id is None:
            raise BillingInvalidError("a review is always for a student")
        beneficiary = await get_or_create_student_account(
            session, tenant_id=tenant_id, student_id=student_id
        )
    else:
        beneficiary = await get_or_create_author_account(session, tenant_id=tenant_id)

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
