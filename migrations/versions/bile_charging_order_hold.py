"""BIL-E: a refund hold keyed by the provider's order; audit values.

Revision ID: bile_charging_order_hold
Revises: bilpay_creem_disputes
Create Date: 2026-11-15

Charging an action (BIL-E) needs no table of its own: it calls the journal
(BIL-B). One thing is new — the administrator's hold of a purchase's units
before a refund is made in the provider's dashboard (operator 2026-11-14/15):
Creem's API has no refunds, so there is no refund id to key the hold by
until its ``refund.created`` arrives. The hold is keyed by the order instead:

* ``billing_refunds.provider_order_id`` — new, nullable: the provider's order
  of a hold by order (Creem: ``order.id``, the batch's
  ``provider_transaction_id``); NULL for any other refund or dispute.
* ``billing_refunds.provider_adjustment_id`` — nullable now: a hold by order
  has none until ``refund.created`` names the refund (then it is written).
  ``ck_billing_refunds_key``: one of the two is always there.
* ``uq_billing_refunds_order_hold`` — one HELD hold per (provider, order).
* ``billing_audit_log.action`` — comment only: gains ``refund.order_hold`` and
  ``refund.order_release``.
* ``billing_movements.kind`` — no change: the cost over a reserve that the
  platform bears is a ``platform_covered`` movement, the kind that exists.

Column comments are copied from the ORM (``storage.orm.BillingRefund``,
``BillingAuditEntry``). Hand-written; ``--autogenerate`` is not used on this
database (``DD-L4-A``).

Downgrade: refused while a hold by order exists (the schema below this
revision has no key for it). Otherwise it drops the index, the check and the
column, makes the adjustment id required again and restores the comments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bile_charging_order_hold"
down_revision: str | Sequence[str] | None = "bilpay_creem_disputes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDER = (
    "A hold by order (BIL-E): the provider's order (Creem: order.id, the "
    "batch's provider_transaction_id) whose units the administrator holds "
    "before refunding it in the provider's dashboard; one held per order. "
    "NULL for any other refund or dispute."
)
_ADJUSTMENT_NEW = (
    "The provider's adjustment (refund) id, or its dispute id; unique per "
    "provider. NULL for a hold by order (BIL-E) until the provider's "
    "refund.created names the refund."
)
_ADJUSTMENT_OLD = (
    "The provider's adjustment (refund) id, or its dispute id; unique per provider."
)
_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit, dispute.lost, dispute.won, refund.order_hold, "
    "refund.order_release."
)
_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit, dispute.lost, dispute.won."
)


def upgrade() -> None:
    op.add_column(
        "billing_refunds",
        sa.Column("provider_order_id", sa.String(64), nullable=True, comment=_ORDER),
    )
    op.alter_column(
        "billing_refunds",
        "provider_adjustment_id",
        existing_type=sa.String(64),
        nullable=True,
        comment=_ADJUSTMENT_NEW,
        existing_comment=_ADJUSTMENT_OLD,
    )
    op.create_check_constraint(
        "ck_billing_refunds_key",
        "billing_refunds",
        "provider_adjustment_id IS NOT NULL OR provider_order_id IS NOT NULL",
    )
    op.create_index(
        "uq_billing_refunds_order_hold",
        "billing_refunds",
        ["provider", "provider_order_id"],
        unique=True,
        postgresql_where=sa.text("state = 'held' AND provider_order_id IS NOT NULL"),
    )
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        comment=_ACTION_NEW,
        existing_comment=_ACTION_OLD,
    )


def downgrade() -> None:
    holds = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM billing_refunds "
                "WHERE provider_order_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if holds:
        raise RuntimeError(
            f"{holds} hold(s) by order exist in billing_refunds; the schema below "
            "bile_charging_order_hold has no key for them — refusing to downgrade"
        )
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        comment=_ACTION_OLD,
        existing_comment=_ACTION_NEW,
    )
    op.drop_index("uq_billing_refunds_order_hold", table_name="billing_refunds")
    op.drop_constraint("ck_billing_refunds_key", "billing_refunds", type_="check")
    op.alter_column(
        "billing_refunds",
        "provider_adjustment_id",
        existing_type=sa.String(64),
        nullable=False,
        comment=_ADJUSTMENT_OLD,
        existing_comment=_ADJUSTMENT_NEW,
    )
    op.drop_column("billing_refunds", "provider_order_id")
