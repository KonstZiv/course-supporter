"""Б-3: the provider's figures of a purchase, and the school's share rate.

Revision ID: b3_partner_report
Revises: v1_author_interface_language
Create Date: 2026-11-27

The partner's report (operator 2026-11-27) shows a school what its students
spent on reviews, the tax and the provider's fee in it, and the school's share
at the rate kept in its settings.

``billing_batches`` gains four nullable columns, all in the batch's currency:

* ``provider_tax_minor`` — the tax the provider collected, inside
  ``amount_paid_minor``;
* ``provider_fee_minor`` — the provider's fee (Creem's API does not give it:
  it stays NULL for Creem);
* ``provider_refunded_minor`` — what the provider says it refunded of the
  payment, as of the sync;
* ``provider_synced_at`` — when those figures were last written.

They are written with the credit when the purchase's webhook carries them,
and by ``scripts/billing_admin.py creem-sync``. ``billing_batches`` is
insert-only (BIL-B, the trigger ``billing_batches_insert_only`` on the shared
function ``billing_refuse_update``). The trigger is re-pointed at a function
of its own, ``billing_batches_refuse_update``: it lets an UPDATE through only
when it changes one of those four columns and nothing else — every other
column, now and added later, stays as inserted. ``billing_movements`` and the
other insert-only tables keep the shared function. Two checks: the figures are
never negative, and only a purchase has them.

``billing_author_settings`` gains ``partner_share_bps`` — nullable, 0 to 10000
(hundredths of a per cent); NULL means no rate. The comment of
``billing_audit_log.action`` gains ``batch.provider_sync``.

Column comments are copied from the ORM (``storage.orm``). Hand-written;
``--autogenerate`` is not used on this database (``DD-L4-A``). The upgrade
rewrites no row. The downgrade drops the columns — the provider's figures
(written again by ``creem-sync``) and the rates (set again by
``set-partner-share``) are lost — and gives the trigger back the shared
function.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3_partner_report"
down_revision: str | Sequence[str] | None = "v1_author_interface_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGER = "billing_batches_insert_only"  # bilb_unit_ledger
_SHARED_FUNCTION = "billing_refuse_update"  # bila_billing_accounts
_BATCH_FUNCTION = "billing_batches_refuse_update"

_FIGURES = (
    "provider_tax_minor",
    "provider_fee_minor",
    "provider_refunded_minor",
    "provider_synced_at",
)

_TAX = (
    "Purchase: the tax the provider collected (inside amount_paid_minor), in "
    "minor units of currency; NULL = not known yet (Б-3)."
)
_FEE = (
    "Purchase: the provider's fee, in minor units of currency; NULL = not "
    "known (Creem's API does not give it, Б-3)."
)
_REFUNDED = (
    "Purchase: what the provider says it refunded of the payment, in minor "
    "units of currency, as of provider_synced_at; NULL = not known (Б-3). The "
    "journal's refunds are billing_refunds."
)
_SYNCED = (
    "Purchase: when the provider's figures were last written (the webhook of "
    "the purchase, or billing_admin creem-sync); NULL = never."
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

# The names in the function's body are the constants above, never input.
_FIGURES_ARRAY = "ARRAY[" + ", ".join(f"'{c}'" for c in _FIGURES) + "]"
_BATCH_FUNCTION_SQL = f"""
CREATE FUNCTION {_BATCH_FUNCTION}()
RETURNS TRIGGER AS $$
BEGIN
    IF to_jsonb(NEW) = to_jsonb(OLD)
       OR (to_jsonb(NEW) - {_FIGURES_ARRAY})
          IS DISTINCT FROM (to_jsonb(OLD) - {_FIGURES_ARRAY}) THEN
        RAISE EXCEPTION '%.% is insert-only: UPDATE refused (only the provider''s figures may change)',
            TG_TABLE_SCHEMA, TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _point_trigger_at(function: str) -> None:
    op.execute(f"DROP TRIGGER {_TRIGGER} ON billing_batches")
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON billing_batches "
        f"FOR EACH ROW EXECUTE FUNCTION {function}();"
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
    """Add the provider's figures and the share rate; narrow the batches' guard."""
    for name, comment in (
        ("provider_tax_minor", _TAX),
        ("provider_fee_minor", _FEE),
        ("provider_refunded_minor", _REFUNDED),
    ):
        op.add_column(
            "billing_batches",
            sa.Column(name, sa.BigInteger(), nullable=True, comment=comment),
        )
    op.add_column(
        "billing_batches",
        sa.Column(
            "provider_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_SYNCED,
        ),
    )
    op.create_check_constraint(
        "ck_billing_batches_provider_figures_not_negative",
        "billing_batches",
        "(provider_tax_minor IS NULL OR provider_tax_minor >= 0) "
        "AND (provider_fee_minor IS NULL OR provider_fee_minor >= 0) "
        "AND (provider_refunded_minor IS NULL OR provider_refunded_minor >= 0)",
    )
    op.create_check_constraint(
        "ck_billing_batches_provider_figures_purchase",
        "billing_batches",
        "source = 'purchase' OR (provider_tax_minor IS NULL "
        "AND provider_fee_minor IS NULL AND provider_refunded_minor IS NULL "
        "AND provider_synced_at IS NULL)",
    )
    op.execute(_BATCH_FUNCTION_SQL)
    _point_trigger_at(_BATCH_FUNCTION)

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
    """Drop them (lossy: module docstring); the guard is the shared one again."""
    _action_comment(_ACTION_OLD, _ACTION_NEW)
    op.drop_constraint(
        "ck_billing_author_settings_partner_share",
        "billing_author_settings",
        type_="check",
    )
    op.drop_column("billing_author_settings", "partner_share_bps")

    _point_trigger_at(_SHARED_FUNCTION)
    op.execute(f"DROP FUNCTION {_BATCH_FUNCTION}()")
    for name in (
        "ck_billing_batches_provider_figures_purchase",
        "ck_billing_batches_provider_figures_not_negative",
    ):
        op.drop_constraint(name, "billing_batches", type_="check")
    for column in reversed(_FIGURES):
        op.drop_column("billing_batches", column)
