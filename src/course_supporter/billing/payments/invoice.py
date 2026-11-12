"""A contract invoice paid — the B2B channel (BIL-PAY).

Purpose:
    A partner platform pays for its students by a contract invoice instead of
    a checkout. In the system the partner is a tenant with an author account;
    the reviews of its students are put on that account by a payment rule
    (BIL-A). So a paid invoice is credited to the tenant's author account the
    way a provider's purchase is — :func:`.ledger.credit_purchase` — with
    ``provider = invoice`` and the invoice's number as both the transaction
    and the line: the same number twice changes nothing.

    The administrator credits it (``scripts/billing_admin.py credit-invoice``,
    ``--actor`` required); the batch and its audit entry
    (``batch.invoice_credit``) land in one transaction. A refund of an
    invoice is not built.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import admin, audit, ledger
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import AuditAction
from course_supporter.billing.payments.port import MAX_ID_LENGTH

INVOICE_PROVIDER: Final = "invoice"
"""The journal's ``provider`` of an invoice; no adapter may take this name."""


async def credit_invoice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    units: int,
    amount_minor: int,
    currency: str,
    invoice: str,
    actor: str,
    now: datetime | None = None,
) -> ledger.Credit:
    """Credit a paid invoice to the tenant's author account (created on first
    need), with its audit entry.

    Idempotent by the invoice's number: a repeat with the same figures returns
    the batch there is and writes nothing, not even an audit entry.

    Raises:
        BillingInvalidError: no actor; no number, or a longer one than the
            journal keeps; the figures (:func:`.ledger.credit_purchase`).
        BillingNotFoundError: no such live tenant.
        BillingConflictError: the number credited before with other figures
            (or to another tenant).
    """
    actor = audit.require_actor(actor)
    number = invoice.strip()
    if not 0 < len(number) <= MAX_ID_LENGTH:
        raise BillingInvalidError(
            f"an invoice number is 1 to {MAX_ID_LENGTH} characters"
        )
    account = await admin.owner_account(session, tenant_id=tenant_id, create=True)
    assert account is not None  # created on first need
    done = await ledger.credit_purchase(
        session,
        account_id=account.id,
        units=units,
        amount_paid_minor=amount_minor,
        currency=currency,
        provider=INVOICE_PROVIDER,
        provider_transaction_id=number,
        provider_line_id=number,
        now=now,
    )
    if done.created:
        await audit.record(
            session,
            actor=actor,
            action=AuditAction.BATCH_INVOICE_CREDIT,
            entity="batch",
            entity_id=done.batch.id,
            before=None,
            after=audit.batch_snapshot(done.batch),
        )
    return done
