"""Destruction of billing records five years after anonymization (BIL-B part 2).

Purpose:
    The operator's rule (ratified): the billing records of an account are kept
    for five years FROM THE DAY ITS OWNER WAS ANONYMIZED
    (``billing_accounts.anonymized_at``), then destroyed for good. An account
    that was never anonymized — active, or closed without anonymization — is
    never destroyed: only ``status = 'closed' AND anonymized_at <= cutoff``
    qualifies, and the condition is checked again under the row's lock.

What goes, for one account:
    its movements, refunds, reservations, differences, batches, consents,
    author settings, the payment rules it is the payer of, the webhook events
    of payment providers that name it (``billing_provider_events.account_id``),
    and the audit entries about it, its batches, its refunds or those rules —
    all or nothing (one SAVEPOINT); then, in a SAVEPOINT of its own, the
    account row itself.

Webhook events that name no account (BIL-PAY):
    A provider's event row without an account — a refund's, an event of no
    interest, a purchase that named none — or naming an account there is no
    longer (or never was) goes five years after it arrived (``received_at``):
    :func:`purge_orphan_events`, in the same nightly task.

What stays, and how long (operator 2026-11-06, option b):
    The foreign keys inside billing have no ON DELETE action. A row of
    ANOTHER account may still point at this one — today the one way is a
    movement naming it as the beneficiary ("за кого": an author paid for
    this student's review), and that movement belongs to the author's
    journal. Then Postgres refuses to delete the account row, and only that
    row stays: anonymized, it holds an opaque id, the kind, the state and the
    dates, nothing of the owner. It is "kept as a reference" and stays due, so
    each night's run tries again; once the rows pointing at it are destroyed
    with their own account, the next run deletes it (or the same run, by a
    second pass over the batch). Kept rows are counted in the run's INFO line.

    Should any of the account's OWN rows be pointed at from outside (not done
    by any code path today), nothing of the account is deleted: it is
    reported as blocked, with a WARNING.

The insert-only triggers on batches, movements, consents and the audit log
refuse UPDATE only (``BEFORE UPDATE``); DELETE is allowed on purpose, for this.

The functions flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, and_, delete, exists, or_, select
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
    BillingProviderEvent,
    BillingRefund,
    BillingReservation,
)

_FOREIGN_KEY_VIOLATION = "23503"


@dataclass(frozen=True, slots=True)
class Purged:
    """One account's rows deleted, per table (``billing_accounts`` included)."""

    account_id: uuid.UUID
    rows: dict[str, int]
    kept_by: str | None = None
    """The foreign key that kept the account row as a reference; None when
    the row went too."""

    @property
    def total(self) -> int:
        """All rows deleted for the account."""
        return sum(self.rows.values())


@dataclass(frozen=True, slots=True)
class Kept:
    """An account row left as a reference: everything else of it is gone."""

    account_id: uuid.UUID
    constraint: str


@dataclass(frozen=True, slots=True)
class Blocked:
    """An account whose own rows are pointed at; nothing of it deleted."""

    account_id: uuid.UUID
    constraint: str


@dataclass(slots=True)
class PurgeOutcome:
    """What one batch of due accounts came to."""

    rows: int = 0
    """Rows deleted, account rows included."""
    destroyed: list[uuid.UUID] = field(default_factory=list)
    """Accounts gone entirely, the account row included."""
    kept: list[Kept] = field(default_factory=list)
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


async def _delete_own_rows(
    session: AsyncSession, account_id: uuid.UUID
) -> dict[str, int]:
    """Every billing row of the account except the account row."""
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
    refund_ids = list(
        (
            await session.execute(
                select(BillingRefund.id).where(BillingRefund.account_id == account_id)
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
        (BillingProviderEvent, BillingProviderEvent.account_id == account_id),
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
                and_(
                    BillingAuditEntry.entity == "refund",
                    BillingAuditEntry.entity_id.in_(refund_ids),
                ),
            ),
        ),
    )
    rows: dict[str, int] = {}
    for model, condition in steps:
        result = await session.execute(
            delete(model).where(condition).execution_options(synchronize_session=False)
        )
        rows[model.__tablename__] = result.rowcount  # type: ignore[attr-defined]
    return rows


def _foreign_key(exc: IntegrityError) -> str:
    """The violated foreign key's name; re-raise any other integrity error."""
    if getattr(exc.orig, "sqlstate", None) != _FOREIGN_KEY_VIOLATION:
        raise exc
    diag = getattr(exc.orig, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


async def purge_account(
    session: AsyncSession, account_id: uuid.UUID, *, cutoff: datetime
) -> Purged | Blocked | None:
    """Destroy one due account (module docstring).

    Returns None when the account is not (or no longer) due; a
    :class:`Purged` whose ``kept_by`` names the foreign key when the account
    row stays as a reference.
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
            rows = await _delete_own_rows(session, account_id)
    except IntegrityError as exc:
        return Blocked(account_id, _foreign_key(exc))
    try:
        async with session.begin_nested():
            deleted = await session.execute(
                delete(BillingAccount)
                .where(BillingAccount.id == account_id)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError as exc:
        return Purged(account_id, rows, kept_by=_foreign_key(exc))
    rows[BillingAccount.__tablename__] = deleted.rowcount  # type: ignore[attr-defined]
    return Purged(account_id, rows)


async def purge_accounts(
    session: AsyncSession, account_ids: Sequence[uuid.UUID], *, cutoff: datetime
) -> PurgeOutcome:
    """Destroy each due account of a batch; retry the rest once.

    The second pass deletes an account row that only another account of the
    same batch pointed at, when that one came later in the first pass.
    """
    outcome = PurgeOutcome()

    async def one_pass(ids: Sequence[uuid.UUID]) -> tuple[list[Kept], list[Blocked]]:
        kept: list[Kept] = []
        blocked: list[Blocked] = []
        for account_id in ids:
            done = await purge_account(session, account_id, cutoff=cutoff)
            if isinstance(done, Blocked):
                blocked.append(done)
            elif isinstance(done, Purged):
                outcome.rows += done.total
                if done.kept_by is None:
                    outcome.destroyed.append(account_id)
                else:
                    kept.append(Kept(account_id, done.kept_by))
        return kept, blocked

    kept, blocked = await one_pass(account_ids)
    if (kept or blocked) and outcome.destroyed:  # what went may free the rest
        kept, blocked = await one_pass(
            [k.account_id for k in kept] + [b.account_id for b in blocked]
        )
    outcome.kept, outcome.blocked = kept, blocked
    return outcome


def _orphan_event_due(cutoff: datetime) -> list[ColumnElement[bool]]:
    has_account = exists().where(BillingAccount.id == BillingProviderEvent.account_id)
    return [
        BillingProviderEvent.received_at <= cutoff,
        or_(BillingProviderEvent.account_id.is_(None), ~has_account),
    ]


async def purge_orphan_events(
    session: AsyncSession,
    *,
    cutoff: datetime,
    after: uuid.UUID | None,
    limit: int,
) -> tuple[int, uuid.UUID | None]:
    """Delete up to ``limit`` due provider events without a live account.

    Due: arrived at or before ``cutoff`` and naming no account, or one that
    is gone. By id above ``after``; returns the rows deleted and the last id
    seen (None when there were none), the cursor of the next call.
    """
    query = select(BillingProviderEvent.id).where(*_orphan_event_due(cutoff))
    if after is not None:
        query = query.where(BillingProviderEvent.id > after)
    ids = list(
        (
            await session.execute(query.order_by(BillingProviderEvent.id).limit(limit))
        ).scalars()
    )
    if not ids:
        return 0, None
    deleted = await session.execute(
        delete(BillingProviderEvent)
        .where(BillingProviderEvent.id.in_(ids))
        .execution_options(synchronize_session=False)
    )
    return deleted.rowcount, ids[-1]  # type: ignore[attr-defined]
