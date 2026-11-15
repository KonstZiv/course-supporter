"""BIL-PAY: the purchase screens — the test-purchase mark, checkouts by our own number.

Revision ID: bilpay_purchase_screens
Revises: bilpay_creem_disputes
Create Date: 2026-11-15

* ``billing_accounts.test_purchase_allowed`` — new, false by default. While a
  payment provider is in its test mode (on production, for the provider's
  review of the shop: operator 2026-11-15), only an account so marked may buy;
  the administrator sets and clears the mark (``scripts/billing_admin.py
  test-purchase``), audited.
* ``billing_audit_log.action`` — comments only: it gains
  ``account.test_purchase``. The column has no check constraint: the comment
  is where the actions are listed.
* ``billing_checkouts`` — new: one row per checkout a payer opened, under the
  platform's own number (``id``, the ``checkout_ref`` of the return address
  and of the provider's custom data): the account, the package, the provider
  and its id of the checkout, and — once the purchase's webhook credited it —
  the batch (``batch_id``, unique). The link is the one change a row takes:
  ``billing_checkouts_link_once`` refuses any other UPDATE, and a second one.
  The journal itself (batches, movements) stays insert-only and untouched.

Column and table comments are copied from the ORM (``storage.orm``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: refused while an account is marked or a checkout exists — the
schema below this revision has no place for either, and dropping them would
lose who may buy in test mode, or which purchase answered which checkout.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bilpay_purchase_screens"
down_revision: str | Sequence[str] | None = "bilpay_creem_disputes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_NAME = "'^[a-z][a-z0-9_]{0,31}$'"
_LINK_ONCE = "billing_checkouts_link_once"

# ── Comments: the ORM's after this revision, the ones before it ──

_TEST_PURCHASE = (
    "True: the account may buy while the payment provider serving the purchase "
    "is in its test mode (the provider's review of the shop, our own tests). "
    "Set and cleared by the administrator only (scripts/billing_admin.py "
    "test-purchase), audited."
)
_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, account.test_purchase, "
    "batch.admin_credit, batch.invoice_credit, dispute.lost, dispute.won."
)
_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit, dispute.lost, dispute.won."
)
_CHECKOUTS = (
    "Checkouts opened at payment providers, by the platform's own number "
    "(BIL-PAY): the account, the package, the provider and its id of the "
    "checkout; the batch the purchase credited."
)


def _action_comment(comment: str) -> None:
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=comment,
    )


def _checkouts() -> None:
    op.create_table(
        "billing_checkouts",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            comment="The checkout's own number (checkout_ref): in the return "
            "address and in the provider's custom data of the checkout.",
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("billing_accounts.id"),
            nullable=False,
            comment="The account the units go to.",
        ),
        sa.Column(
            "package_id",
            sa.String(64),
            nullable=False,
            comment="The package bought (config/billing_packages.yaml).",
        ),
        sa.Column(
            "provider",
            sa.String(32),
            nullable=False,
            comment="The provider's adapter name (BILLING_PROVIDERS).",
        ),
        sa.Column(
            "provider_checkout_id",
            sa.String(64),
            nullable=False,
            comment="The provider's id of the checkout.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the checkout was opened.",
        ),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("billing_batches.id"),
            nullable=True,
            comment="The batch the purchase credited; NULL until the provider "
            "reports it paid. Set once, by the purchase's webhook.",
        ),
        sa.CheckConstraint(
            f"provider ~ {_PROVIDER_NAME}", name="ck_billing_checkouts_provider"
        ),
        sa.UniqueConstraint("batch_id", name="uq_billing_checkouts_batch"),
        comment=_CHECKOUTS,
    )
    op.create_index(
        "ix_billing_checkouts_account_id", "billing_checkouts", ["account_id"]
    )
    # The one UPDATE a checkout takes: its batch, set once; nothing else moves.
    op.execute(
        f"""
CREATE FUNCTION {_LINK_ONCE}() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.batch_id IS NOT NULL
        OR NEW.batch_id IS NULL
        OR (NEW.id, NEW.account_id, NEW.package_id, NEW.provider,
            NEW.provider_checkout_id, NEW.created_at)
           IS DISTINCT FROM
           (OLD.id, OLD.account_id, OLD.package_id, OLD.provider,
            OLD.provider_checkout_id, OLD.created_at)
    THEN
        RAISE EXCEPTION '%.%: only batch_id may be set, and once',
            TG_TABLE_SCHEMA, TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        f"CREATE TRIGGER {_LINK_ONCE} BEFORE UPDATE ON billing_checkouts "
        f"FOR EACH ROW EXECUTE FUNCTION {_LINK_ONCE}();"
    )


def upgrade() -> None:
    op.add_column(
        "billing_accounts",
        sa.Column(
            "test_purchase_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=_TEST_PURCHASE,
        ),
    )
    _action_comment(_ACTION_NEW)
    _checkouts()


def downgrade() -> None:
    bind = op.get_bind()
    marked = bind.scalar(
        sa.text("SELECT count(*) FROM billing_accounts WHERE test_purchase_allowed")
    )
    checkouts = bind.scalar(sa.text("SELECT count(*) FROM billing_checkouts"))
    if marked or checkouts:
        msg = (
            f"refused: {marked} account(s) marked for test purchases and "
            f"{checkouts} checkout(s) exist; the schema below "
            "bilpay_purchase_screens cannot hold them"
        )
        raise RuntimeError(msg)
    op.drop_table("billing_checkouts")  # its trigger and indexes go with it
    op.execute(f"DROP FUNCTION {_LINK_ONCE}()")
    _action_comment(_ACTION_OLD)
    op.drop_column("billing_accounts", "test_purchase_allowed")
