"""BIL-PAY part 2: disputes hold units, refunds made at the provider, authors' consent.

Revision ID: bilpay_creem_disputes
Revises: bilpay_payment_port
Create Date: 2026-11-14

The first real payment provider (Creem) and two decisions of the operator
(2026-11-12) that come with it:

* ``billing_refunds.kind`` — new: ``refund`` (asked of the provider, or made
  at the provider and followed by the journal) | ``dispute`` (a chargeback:
  the batch's free units are held until the dispute ends — lost, they leave
  the batch; won, they are free again). Existing rows are refunds (the
  server default). The comments of ``amount_minor``, ``provider_adjustment_id``
  and ``state`` and of the table say what a dispute and a refund made at the
  provider keep there.
* ``billing_consents.channel`` — also ``api``: an author agrees to the terms
  through the API with the tenant's key, once per revision, as a student does
  in the portal.
* ``billing_provider_events`` — comments only: ``state`` (a chargeback is
  processed now, no longer recorded) and ``provider_adjustment_id`` (a
  chargeback keeps its dispute id there).
* ``billing_audit_log`` — comments only: ``action`` gains ``dispute.lost`` and
  ``dispute.won``, ``entity`` gains ``refund``.

Column and table comments are copied from the ORM (``storage.orm.Billing*``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: refused while a dispute row or an author's consent through the API
exists — the schema below this revision would read a dispute as a refund, and
has no channel for that consent. Otherwise it drops ``kind``, narrows the
channel back and restores the comments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bilpay_creem_disputes"
down_revision: str | Sequence[str] | None = "bilpay_payment_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ── Comments: the ORM's after this revision, the ones before it ──

_REFUNDS_TABLE_NEW = (
    "Refund or dispute of unused units of a purchase batch (BIL-B, BIL-PAY): "
    "held while the provider reviews it (or the dispute lasts), then approved "
    "(units gone) or rejected (units back)."
)
_REFUNDS_TABLE_OLD = (
    "Refund of unused units of a purchase batch (BIL-B): held while the "
    "provider reviews it, then approved or rejected."
)
_KIND = (
    "refund (asked of the provider, or made at the provider) | dispute (a "
    "chargeback: units held until it ends)."
)
_AMOUNT_NEW = (
    "Money back, in minor units of the batch's currency: floor(amount_paid_minor "
    "* units / batch units) for a refund asked of the provider and for a "
    "dispute (the worth of the units held); the provider's own figure for a "
    "refund made at the provider."
)
_AMOUNT_OLD = (
    "Money back: floor(amount_paid_minor * units / batch units), in minor "
    "units of the batch's currency."
)
_ADJUSTMENT_NEW = (
    "The provider's adjustment (refund) id, or its dispute id; unique per provider."
)
_ADJUSTMENT_OLD = "The provider's adjustment (refund) id; unique per provider."
_REFUND_STATE_NEW = (
    "held (units blocked while the provider reviews, or while the dispute "
    "lasts) | approved (units gone; a dispute lost) | rejected (units back in "
    "the batch; a dispute won)."
)
_REFUND_STATE_OLD = (
    "held (units blocked while the provider reviews) | approved (units gone) | "
    "rejected (units back in the batch)."
)
_CHANNEL_NEW = (
    "Where the holder agreed: portal (a student) | api (an author, with the "
    "tenant's key)."
)
_CHANNEL_OLD = "Where the holder agreed: portal."
_EVENT_STATE_NEW = (
    "processed (the journal acted) | ignored (not an event billing acts on) | "
    "recorded (kept, no action: chargebacks before BIL-PAY part 2) | failed "
    "(see error; handled again when the provider repeats the event)."
)
_EVENT_STATE_OLD = (
    "processed (the journal acted) | ignored (not an event billing acts on) | "
    "recorded (a chargeback: kept, no action) | failed (see error; handled "
    "again when the provider repeats the event)."
)
_EVENT_ADJUSTMENT_NEW = "Refund: the provider's adjustment id; chargeback: its dispute id."
_EVENT_ADJUSTMENT_OLD = "Refund: the provider's adjustment id."
_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit, dispute.lost, dispute.won."
)
_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit."
)
_ENTITY_NEW = "Kind of row changed: account | author_settings | payment_rule | batch | refund."
_ENTITY_OLD = "Kind of row changed: account | author_settings | payment_rule | batch."


def _comments(
    *,
    refunds_table: str,
    amount: str,
    adjustment: str,
    refund_state: str,
    channel: str,
    event_state: str,
    event_adjustment: str,
    action: str,
    entity: str,
) -> None:
    op.create_table_comment("billing_refunds", refunds_table)
    for table, column, type_, comment, nullable in (
        ("billing_refunds", "amount_minor", sa.BigInteger(), amount, False),
        ("billing_refunds", "provider_adjustment_id", sa.String(64), adjustment, False),
        ("billing_refunds", "state", sa.String(16), refund_state, False),
        ("billing_consents", "channel", sa.String(16), channel, False),
        ("billing_provider_events", "state", sa.String(16), event_state, False),
        (
            "billing_provider_events",
            "provider_adjustment_id",
            sa.String(64),
            event_adjustment,
            True,
        ),
        ("billing_audit_log", "action", sa.String(64), action, False),
        ("billing_audit_log", "entity", sa.String(32), entity, False),
    ):
        op.alter_column(
            table, column, existing_type=type_, existing_nullable=nullable, comment=comment
        )


def upgrade() -> None:
    op.add_column(
        "billing_refunds",
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="refund",
            comment=_KIND,
        ),
    )
    op.create_check_constraint(
        "ck_billing_refunds_kind", "billing_refunds", "kind IN ('refund', 'dispute')"
    )
    op.drop_constraint("ck_billing_consents_channel", "billing_consents", type_="check")
    op.create_check_constraint(
        "ck_billing_consents_channel", "billing_consents", "channel IN ('portal', 'api')"
    )
    _comments(
        refunds_table=_REFUNDS_TABLE_NEW,
        amount=_AMOUNT_NEW,
        adjustment=_ADJUSTMENT_NEW,
        refund_state=_REFUND_STATE_NEW,
        channel=_CHANNEL_NEW,
        event_state=_EVENT_STATE_NEW,
        event_adjustment=_EVENT_ADJUSTMENT_NEW,
        action=_ACTION_NEW,
        entity=_ENTITY_NEW,
    )


def downgrade() -> None:
    bind = op.get_bind()
    disputes = bind.scalar(
        sa.text("SELECT count(*) FROM billing_refunds WHERE kind = 'dispute'")
    )
    api_consents = bind.scalar(
        sa.text("SELECT count(*) FROM billing_consents WHERE channel = 'api'")
    )
    if disputes or api_consents:
        msg = (
            f"refused: {disputes} dispute row(s) and {api_consents} consent(s) "
            "through the API exist; the schema below bilpay_creem_disputes cannot "
            "hold them"
        )
        raise RuntimeError(msg)
    _comments(
        refunds_table=_REFUNDS_TABLE_OLD,
        amount=_AMOUNT_OLD,
        adjustment=_ADJUSTMENT_OLD,
        refund_state=_REFUND_STATE_OLD,
        channel=_CHANNEL_OLD,
        event_state=_EVENT_STATE_OLD,
        event_adjustment=_EVENT_ADJUSTMENT_OLD,
        action=_ACTION_OLD,
        entity=_ENTITY_OLD,
    )
    op.drop_constraint("ck_billing_consents_channel", "billing_consents", type_="check")
    op.create_check_constraint(
        "ck_billing_consents_channel", "billing_consents", "channel IN ('portal')"
    )
    op.drop_constraint("ck_billing_refunds_kind", "billing_refunds", type_="check")
    op.drop_column("billing_refunds", "kind")
