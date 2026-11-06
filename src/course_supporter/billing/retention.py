"""Destruction of billing records five years after anonymization (BIL-B part 2).

Purpose:
    The operator's rule (ratified): the billing records of an account are kept
    for five years FROM THE DAY ITS OWNER WAS ANONYMIZED
    (``billing_accounts.anonymized_at``), then destroyed for good. An account
    that was never anonymized — active, or closed without anonymization — is
    never destroyed: only ``status = 'closed' AND anonymized_at <= cutoff``
    qualifies, and the condition is checked again under the row's lock.

What goes, for one account, all or nothing (one SAVEPOINT):
    its movements, refunds, reservations, differences, batches, consents,
    author settings, the payment rules it is the payer of, the audit entries
    about it, its batches or those rules, and the account itself.

What stops it:
    The foreign keys inside billing have no ON DELETE action. If a row that is
    NOT being destroyed still points at the account — today the one way is a
    movement on ANOTHER account naming it as the beneficiary ("за кого": an
    author paid for this student's review) — Postgres refuses the delete, the
    savepoint is rolled back, and the account is reported as blocked: nothing
    of it is destroyed. Such an account waits until the account pointing at it
    is destroyed in turn (if that one is due in the same batch, a second pass
    over the batch finds it free).

The insert-only triggers on batches, movements, consents and the audit log
refuse UPDATE only (``BEFORE UPDATE``); DELETE is allowed on purpose, for this.

The functions flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import AccountStatus
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingConsent,
    BillingDifference,
    BillingMovement,
    BillingPaymentRule,
    BillingRefund,
    BillingReservation,
)

_FOREIGN_KEY_VIOLATION = "23503"


@dataclass(frozen=True, slots=True)
class Purged:
    """One account destroyed: the rows deleted, per table."""

    account_id: uuid.UUID
    rows: dict[str, int]

    @property
    def total(self) -> int:
        """All rows deleted for the account."""
        return sum(self.rows.values())


@dataclass(frozen=True, slots=True)
class Blocked:
    """An account a row outside it still points at; nothing of it deleted."""

    account_id: uuid.UUID
    constraint: str


@dataclass(slots=True)
class PurgeOutcome:
    """What one batch of due accounts came to."""

    purged: list[Purged] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)


def retention_cutoff(now: datetime, years: int) -> datetime:
    """The latest ``anonymized_at`` that is due at ``now``: ``now - years``."""
    return add_months(now, -12 * years)


def _due(cutoff: datetime) -> list[ColumnElement[bool]]:
    return [
        BillingAccount.status == AccountStatus.CLOSED.value,
        BillingAccount.anonymized_at.is_not(None),
        BillingAccount.anonymized_at <= cutoff,
    ]


async def accounts_due(
    session: AsyncSession,
    *,
    cutoff: datetime,
    after: uuid.UUID | None,
    limit: int,
) -> list[uuid.UUID]:
    """Up to ``limit`` due accounts with ids above ``after``, by id."""
    query = select(BillingAccount.id).where(*_due(cutoff))
    if after is not None:
        query = query.where(BillingAccount.id > after)
    rows = await session.execute(query.order_by(BillingAccount.id).limit(limit))
    return list(rows.scalars())


async def _delete_rows(session: AsyncSession, account_id: uuid.UUID) -> dict[str, int]:
    batch_ids = list(
        (
            await session.execute(
                select(BillingBatch.id).where(BillingBatch.account_id == account_id)
            )
        ).scalars()
    )
    rule_ids = list(
        (
            await session.execute(
                select(BillingPaymentRule.id).where(
                    BillingPaymentRule.payer_account_id == account_id
                )
            )
        ).scalars()
    )
    # Children before parents: movements point at batches, differences,
    # refunds, rules and accounts; refunds at batches.
    steps = (
        (BillingMovement, BillingMovement.account_id == account_id),
        (BillingRefund, BillingRefund.account_id == account_id),
        (BillingReservation, BillingReservation.account_id == account_id),
        (BillingDifference, BillingDifference.account_id == account_id),
        (BillingBatch, BillingBatch.account_id == account_id),
        (BillingConsent, BillingConsent.account_id == account_id),
        (BillingAuthorSettings, BillingAuthorSettings.account_id == account_id),
        (BillingPaymentRule, BillingPaymentRule.payer_account_id == account_id),
        (
            BillingAuditEntry,
            or_(
                and_(
                    BillingAuditEntry.entity.in_(("account", "author_settings")),
                    BillingAuditEntry.entity_id == account_id,
                ),
                and_(
                    BillingAuditEntry.entity == "batch",
                    BillingAuditEntry.entity_id.in_(batch_ids),
                ),
                and_(
                    BillingAuditEntry.entity == "payment_rule",
                    BillingAuditEntry.entity_id.in_(rule_ids),
                ),
            ),
        ),
        (BillingAccount, BillingAccount.id == account_id),
    )
    rows: dict[str, int] = {}
    for model, condition in steps:
        result = await session.execute(
            delete(model).where(condition).execution_options(synchronize_session=False)
        )
        rows[model.__tablename__] = result.rowcount  # type: ignore[attr-defined]
    return rows


async def purge_account(
    session: AsyncSession, account_id: uuid.UUID, *, cutoff: datetime
) -> Purged | Blocked | None:
    """Destroy one due account, all or nothing (module docstring).

    Returns None when the account is not (or no longer) due.
    """
    try:
        async with session.begin_nested():
            due = await session.scalar(
                select(BillingAccount.id)
                .where(BillingAccount.id == account_id, *_due(cutoff))
                .with_for_update()
            )
            if due is None:
                return None
            rows = await _delete_rows(session, account_id)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != _FOREIGN_KEY_VIOLATION:
            raise
        diag = getattr(exc.orig, "diag", None)
        return Blocked(account_id, str(getattr(diag, "constraint_name", "") or ""))
    return Purged(account_id, rows)


async def purge_accounts(
    session: AsyncSession, account_ids: Sequence[uuid.UUID], *, cutoff: datetime
) -> PurgeOutcome:
    """Destroy each due account of a batch; retry the blocked ones once.

    The second pass frees an account that only another account of the same
    batch pointed at, when that one came later in the first pass.
    """
    outcome = PurgeOutcome()

    async def one_pass(ids: Sequence[uuid.UUID]) -> list[Blocked]:
        blocked: list[Blocked] = []
        for account_id in ids:
            done = await purge_account(session, account_id, cutoff=cutoff)
            if isinstance(done, Purged):
                outcome.purged.append(done)
            elif isinstance(done, Blocked):
                blocked.append(done)
        return blocked

    blocked = await one_pass(account_ids)
    if blocked and outcome.purged:  # what went may have freed what was blocked
        blocked = await one_pass([b.account_id for b in blocked])
    outcome.blocked = blocked
    return outcome
