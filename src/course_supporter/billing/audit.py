"""The audit log of administrative changes to billing data (BIL-A).

Every change made through :mod:`.admin`, :mod:`.accounts` or an
administrator's credit of units (:func:`.ledger.credit_admin`) writes one entry
in the same transaction as the change: who (the administrator's ``--actor``),
when, what, which row, the row before and after. The table refuses UPDATE (a
trigger), so an entry, once written, stays as written.

A snapshot is the row as JSON — ids as text, instants in ISO 8601. The
snapshot of an ACCOUNT deliberately leaves out its owner (``tenant_id``,
``student_id``): the log outlives the anonymization of the account and must
not keep the link that the anonymization erases.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import AuditAction
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingPaymentRule,
)


def _instant(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def account_snapshot(account: BillingAccount) -> dict[str, Any]:
    """The account without its owner (module docstring)."""
    return {
        "kind": account.kind,
        "status": account.status,
        "closed_at": _instant(account.closed_at),
        "anonymized_at": _instant(account.anonymized_at),
    }


def settings_snapshot(settings: BillingAuthorSettings) -> dict[str, Any]:
    """What the administrator can change on an author account."""
    return {
        "account_id": _id(settings.account_id),
        "benefit": settings.benefit,
        "monthly_limit_units": settings.monthly_limit_units,
        "benefit_started_at": _instant(settings.benefit_started_at),
        "credit_allowed": settings.credit_allowed,
    }


def rule_snapshot(rule: BillingPaymentRule) -> dict[str, Any]:
    """A payment rule as set, or as ended."""
    return {
        "scope": rule.scope,
        "action_kind": rule.action_kind,
        "tenant_id": _id(rule.tenant_id),
        "course_node_id": _id(rule.course_node_id),
        "student_id": _id(rule.student_id),
        "payer_account_id": _id(rule.payer_account_id),
        "active": rule.active,
        "created_by": rule.created_by,
        "ended_at": _instant(rule.ended_at),
    }


def batch_snapshot(batch: BillingBatch) -> dict[str, Any]:
    """A batch as it arrived (BIL-B); a batch never changes afterwards."""
    return {
        "account_id": _id(batch.account_id),
        "source": batch.source,
        "units": batch.units,
        "created_at": _instant(batch.created_at),
        "expires_at": _instant(batch.expires_at),
        "reason": batch.reason,
    }


def require_actor(actor: str) -> str:
    """The actor, stripped; refuses an empty one before anything changes."""
    stripped = actor.strip()
    if not stripped:
        raise BillingInvalidError("an administrative change needs an actor")
    return stripped


async def record(
    session: AsyncSession,
    *,
    actor: str,
    action: AuditAction,
    entity: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> BillingAuditEntry:
    """Write one audit entry; the caller's transaction commits it."""
    entry = BillingAuditEntry(
        actor=require_actor(actor),
        action=action.value,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
    )
    session.add(entry)
    await session.flush()
    return entry
