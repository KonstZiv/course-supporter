"""Б-3: the provider's figures of a purchase, and the school's share rate.

Revision ID: b3_partner_report
Revises: b4_promotion_funnel
Create Date: 2026-11-27

The partner's report (operator 2026-11-27) shows a school what its students
spent on reviews, the tax and the provider's fee in it, and the school's share
at the rate kept in its settings.

``billing_batch_provider_figures`` — a new table, one row per purchase batch
(``batch_id``: the primary key and a foreign key to ``billing_batches``, no ON
DELETE action, as the other billing keys), in the batch's currency:

* ``tax_minor`` — the tax the provider collected, inside ``amount_paid_minor``;
* ``fee_minor`` — the provider's fee as its API or event gives it (Creem's
  gives none: the report counts it by the tariff);
* ``refunded_minor`` — what the provider says it refunded of the payment;
* ``synced_at`` — when the row was last written.

The three figures are nullable (not known); a check keeps them not negative.
The row is upserted by ``batch_id`` — by the purchase's webhook and by
``scripts/billing_admin.py creem-sync``. ``billing_batches`` is NOT changed:
no column, and its trigger ``billing_batches_insert_only`` stays on the shared
function ``billing_refuse_update`` (BIL-B) — every UPDATE of a batch refused
(decision of the billing session, Б-5 2026-11-27: the batch's guard is not
weakened).

``billing_author_settings`` gains ``partner_share_bps`` — nullable, 0 to 10000
(hundredths of a per cent); NULL means no rate. The comment of
``billing_audit_log.action`` gains ``batch.provider_sync``.

Column and table comments are copied from the ORM (``storage.orm``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).
The upgrade rewrites no row. The downgrade drops the table and the column —
the provider's figures (written again by ``creem-sync``) and the rates (set
again by ``set-partner-share``) are lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3_partner_report"
down_revision: str | Sequence[str] | None = "b4_promotion_funnel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIGURES = "billing_batch_provider_figures"

_TABLE_COMMENT = (
    "What the payment provider says of a purchase's money (Б-3): tax, fee, "
    "refunded part, in the batch's currency. Upserted by batch_id; "
    "billing_batches stays insert-only."
)
_BATCH = "The purchase's batch; one row per batch."
_TAX = (
    "The tax the provider collected (inside the batch's amount_paid_minor), in "
    "minor units of the batch's currency; NULL = not known yet."
)
_FEE = (
    "The provider's fee as its API or event gives it, in minor units of the "
    "batch's currency; NULL = not given (Creem's API gives none: the partner's "
    "report counts it by the tariff)."
)
_REFUNDED = (
    "What the provider says it refunded of the payment, in minor units of the "
    "batch's currency, as of synced_at; NULL = not known. The journal's refunds "
    "are billing_refunds."
)
_SYNCED = (
    "When the figures were last written (the purchase's webhook, or "
    "billing_admin creem-sync)."
)
_SHARE = (
    "The school's share of the net money its students spent on reviews, in "
    "basis points (0-10000 = 0-100 %), for the partner's report (Б-3); NULL = "
    "no rate set. Paid out nowhere by the platform."
)

_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, account.test_purchase, "
    "batch.admin_credit, batch.invoice_credit, dispute.lost, dispute.won, "
    "refund.order_hold, refund.order_release, batch.test_void, "
    "account.create."
)
_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, account.test_purchase, "
    "batch.admin_credit, batch.invoice_credit, dispute.lost, dispute.won, "
    "refund.order_hold, refund.order_release, batch.test_void, "
    "account.create, batch.provider_sync."
)


def _action_comment(comment: str, existing: str) -> None:
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=comment,
        existing_comment=existing,
    )


def upgrade() -> None:
    """The provider's figures table and the share rate; batches untouched."""
    op.create_table(
        _FIGURES,
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("billing_batches.id"),
            primary_key=True,
            comment=_BATCH,
        ),
        sa.Column("tax_minor", sa.BigInteger(), nullable=True, comment=_TAX),
        sa.Column("fee_minor", sa.BigInteger(), nullable=True, comment=_FEE),
        sa.Column("refunded_minor", sa.BigInteger(), nullable=True, comment=_REFUNDED),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, comment=_SYNCED
        ),
        sa.CheckConstraint(
            "(tax_minor IS NULL OR tax_minor >= 0) "
            "AND (fee_minor IS NULL OR fee_minor >= 0) "
            "AND (refunded_minor IS NULL OR refunded_minor >= 0)",
            name="ck_billing_batch_provider_figures_not_negative",
        ),
        comment=_TABLE_COMMENT,
    )

    op.add_column(
        "billing_author_settings",
        sa.Column("partner_share_bps", sa.Integer(), nullable=True, comment=_SHARE),
    )
    op.create_check_constraint(
        "ck_billing_author_settings_partner_share",
        "billing_author_settings",
        "partner_share_bps IS NULL OR partner_share_bps BETWEEN 0 AND 10000",
    )
    _action_comment(_ACTION_NEW, _ACTION_OLD)


def downgrade() -> None:
    """Drop them (lossy: module docstring)."""
    _action_comment(_ACTION_OLD, _ACTION_NEW)
    op.drop_constraint(
        "ck_billing_author_settings_partner_share",
        "billing_author_settings",
        type_="check",
    )
    op.drop_column("billing_author_settings", "partner_share_bps")
    op.drop_table(_FIGURES)
