"""Ending a payment rule — the one path, whoever ends it (BIL-A).

The administrator ends a rule by its id (:func:`.admin.end_payment_rule`);
the anonymization of a deleted owner ends the rules about that owner
(:mod:`.accounts`). Both go through :func:`end_rule`, so an ended rule looks
the same and leaves the same ``payment_rule.end`` audit entry either way.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit
from course_supporter.billing.kinds import AuditAction
from course_supporter.storage.orm import BillingPaymentRule


async def end_rule(
    session: AsyncSession, rule: BillingPaymentRule, *, actor: str, now: datetime
) -> None:
    """End an ACTIVE rule (``active = false``, ``ended_at = now``) and audit it.

    The caller has checked the actor and that the rule is active. The rule's
    target ids stay: the check constraint needs them, and once the owner is
    deleted they name rows that no longer exist.
    """
    before = audit.rule_snapshot(rule)
    rule.active = False
    rule.ended_at = now
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
