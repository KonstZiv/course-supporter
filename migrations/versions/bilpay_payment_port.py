"""BIL-PAY: the payment port — provider-neutral purchases and refunds, provider events.

Revision ID: bilpay_payment_port
Revises: bilb_unit_ledger
Create Date: 2026-11-12

Billing sells through several payment providers side by side (merchants of
record, each behind an adapter of its own) and, for B2B, by a contract
invoice. The journal no longer names one provider:

* ``billing_batches``: ``paddle_transaction_id`` → ``provider_transaction_id``,
  ``paddle_line_id`` → ``provider_line_id``; a new ``provider`` (the adapter's
  name, or ``invoice``) — required on a purchase, NULL on an admin credit. The
  key of idempotency is now ``(provider, provider_line_id)``
  (``uq_billing_batches_provider_line``) instead of the Paddle line.
* ``billing_refunds``: ``paddle_adjustment_id`` → ``provider_adjustment_id``,
  a new ``provider`` (required); one refund per ``(provider,
  provider_adjustment_id)``.
* ``billing_provider_events`` — new: one row per webhook event of a provider,
  unique by ``(provider, provider_event_id)``; the normalized fields the
  journal needs, how the event was handled, the SHA-256 of the raw body —
  never the body, never personal data. No foreign key (``account_id`` names an
  account that may not exist); no insert-only trigger (a failed event is
  handled again), and no cascade reaches it.
* ``billing_audit_log.action`` — its comment gains ``batch.invoice_credit``.

Existing rows: before this revision every purchase and every refund was
Paddle's — the columns said so — so they get ``provider = 'paddle'``. The
backfill of ``billing_batches`` is the one UPDATE that table ever takes: its
insert-only trigger is off for that statement only, inside this migration's
transaction. No code path wrote a purchase or a refund before this revision,
so on a deployed database the backfill finds nothing.

Column and table comments are copied from the ORM (``storage.orm.Billing*``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: refused while any batch or refund belongs to a provider other than
Paddle (an invoice, a test provider) — the schema below this revision would
read it as a Paddle purchase, refundable through Paddle. Otherwise it renames
the columns back, drops ``provider`` and drops ``billing_provider_events``
with its rows (the record of which events were handled; the journal keeps
what they did).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bilpay_payment_port"
down_revision: str | Sequence[str] | None = "bilb_unit_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_NAME = "'^[a-z][a-z0-9_]{0,31}$'"
_INSERT_ONLY_TRIGGER = "billing_batches_insert_only"  # bilb_unit_ledger

# ── Comments: the ORM's after this revision, the ones before it ──

_SOURCE_NEW = (
    "purchase (paid through a payment provider or by a contract invoice) | "
    "admin_credit (granted by the administrator, never refunded)."
)
_SOURCE_OLD = (
    "purchase (paid through Paddle, refundable) | admin_credit "
    "(granted by the administrator, never refunded)."
)
_BATCH_PROVIDER = (
    "Purchase: who took the payment, a payment provider's adapter name (e.g. "
    "paddle) or invoice (a contract invoice). NULL on an admin credit."
)
_TRANSACTION_NEW = "Purchase: the provider's transaction id (an invoice: its number)."
_TRANSACTION_OLD = "Purchase: Paddle transaction id; set by the Paddle adapter."
_LINE_NEW = (
    "Purchase: the provider's id of the package's line within the transaction "
    "(an invoice: its number); unique per provider."
)
_LINE_OLD = (
    "Purchase: Paddle line item id of the package within the transaction; set "
    "by the Paddle adapter."
)
_REFUNDS_NEW = (
    "Refund of unused units of a purchase batch (BIL-B): held while the "
    "provider reviews it, then approved or rejected."
)
_REFUNDS_OLD = (
    "Refund of unused units of a purchase batch (BIL-B): held while Paddle "
    "reviews it, then approved or rejected."
)
_REFUND_PROVIDER = "The payment provider the refund goes through: the batch's."
_ADJUSTMENT_NEW = "The provider's adjustment (refund) id; unique per provider."
_ADJUSTMENT_OLD = "Paddle adjustment (refund) id."
_STATE_NEW = (
    "held (units blocked while the provider reviews) | approved (units gone) | "
    "rejected (units back in the batch)."
)
_STATE_OLD = (
    "held (units blocked while Paddle reviews) | approved (units gone) | "
    "rejected (units back in the batch)."
)
_DECIDED_NEW = "When the provider's decision was recorded; NULL while held."
_DECIDED_OLD = "When Paddle's decision was recorded; NULL while held."
_AUDIT_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit, "
    "batch.invoice_credit."
)
_AUDIT_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit."
)

# ── Check constraints of billing_batches, after and before ──

_PURCHASE_NEW = (
    "source <> 'purchase' OR (provider IS NOT NULL "
    "AND amount_paid_minor IS NOT NULL AND currency IS NOT NULL "
    "AND reason IS NULL AND actor IS NULL)"
)
_PURCHASE_OLD = (
    "source <> 'purchase' OR (amount_paid_minor IS NOT NULL "
    "AND currency IS NOT NULL AND reason IS NULL AND actor IS NULL)"
)
_ADMIN_CREDIT_NEW = (
    "source <> 'admin_credit' OR (reason IS NOT NULL AND actor IS NOT NULL "
    "AND amount_paid_minor IS NULL AND currency IS NULL "
    "AND provider IS NULL AND provider_transaction_id IS NULL)"
)
_ADMIN_CREDIT_OLD = (
    "source <> 'admin_credit' OR (reason IS NOT NULL AND actor IS NOT NULL "
    "AND amount_paid_minor IS NULL AND currency IS NULL "
    "AND paddle_transaction_id IS NULL)"
)


def _comment(
    table: str,
    column: str,
    type_: sa.types.TypeEngine,
    comment: str,
    *,
    nullable: bool,
    new_name: str | None = None,
) -> None:
    op.alter_column(
        table,
        column,
        new_column_name=new_name,
        existing_type=type_,
        existing_nullable=nullable,
        comment=comment,
    )


def _batches_up() -> None:
    _comment(
        "billing_batches",
        "paddle_transaction_id",
        sa.String(64),
        _TRANSACTION_NEW,
        nullable=True,
        new_name="provider_transaction_id",
    )
    _comment(
        "billing_batches",
        "paddle_line_id",
        sa.String(64),
        _LINE_NEW,
        nullable=True,
        new_name="provider_line_id",
    )
    _comment("billing_batches", "source", sa.String(16), _SOURCE_NEW, nullable=False)
    op.add_column(
        "billing_batches",
        sa.Column("provider", sa.String(32), nullable=True, comment=_BATCH_PROVIDER),
    )
    # Every purchase so far was Paddle's (module docstring). The table refuses
    # UPDATE; this one statement is let through, in this transaction only.
    op.execute(f"ALTER TABLE billing_batches DISABLE TRIGGER {_INSERT_ONLY_TRIGGER}")
    op.execute(
        "UPDATE billing_batches SET provider = 'paddle' WHERE source = 'purchase'"
    )
    op.execute(f"ALTER TABLE billing_batches ENABLE TRIGGER {_INSERT_ONLY_TRIGGER}")

    for name in (
        "ck_billing_batches_paddle_ids",
        "ck_billing_batches_purchase",
        "ck_billing_batches_admin_credit",
    ):
        op.drop_constraint(name, "billing_batches", type_="check")
    op.create_check_constraint(
        "ck_billing_batches_purchase", "billing_batches", _PURCHASE_NEW
    )
    op.create_check_constraint(
        "ck_billing_batches_admin_credit", "billing_batches", _ADMIN_CREDIT_NEW
    )
    op.create_check_constraint(
        "ck_billing_batches_provider",
        "billing_batches",
        f"provider IS NULL OR provider ~ {_PROVIDER_NAME}",
    )
    op.create_check_constraint(
        "ck_billing_batches_provider_ids",
        "billing_batches",
        "(provider_transaction_id IS NULL) = (provider_line_id IS NULL)",
    )
    op.drop_index("uq_billing_batches_paddle_line", table_name="billing_batches")
    op.create_index(
        "uq_billing_batches_provider_line",
        "billing_batches",
        ["provider", "provider_line_id"],
        unique=True,
        postgresql_where=sa.text("provider_line_id IS NOT NULL"),
    )


def _batches_down() -> None:
    op.drop_index("uq_billing_batches_provider_line", table_name="billing_batches")
    for name in (
        "ck_billing_batches_provider_ids",
        "ck_billing_batches_provider",
        "ck_billing_batches_purchase",
        "ck_billing_batches_admin_credit",
    ):
        op.drop_constraint(name, "billing_batches", type_="check")
    op.drop_column("billing_batches", "provider")
    _comment(
        "billing_batches",
        "provider_transaction_id",
        sa.String(64),
        _TRANSACTION_OLD,
        nullable=True,
        new_name="paddle_transaction_id",
    )
    _comment(
        "billing_batches",
        "provider_line_id",
        sa.String(64),
        _LINE_OLD,
        nullable=True,
        new_name="paddle_line_id",
    )
    _comment("billing_batches", "source", sa.String(16), _SOURCE_OLD, nullable=False)
    op.create_check_constraint(
        "ck_billing_batches_purchase", "billing_batches", _PURCHASE_OLD
    )
    op.create_check_constraint(
        "ck_billing_batches_admin_credit", "billing_batches", _ADMIN_CREDIT_OLD
    )
    op.create_check_constraint(
        "ck_billing_batches_paddle_ids",
        "billing_batches",
        "(paddle_transaction_id IS NULL) = (paddle_line_id IS NULL)",
    )
    op.create_index(
        "uq_billing_batches_paddle_line",
        "billing_batches",
        ["paddle_transaction_id", "paddle_line_id"],
        unique=True,
        postgresql_where=sa.text("paddle_transaction_id IS NOT NULL"),
    )


def _refunds_up() -> None:
    _comment(
        "billing_refunds",
        "paddle_adjustment_id",
        sa.String(64),
        _ADJUSTMENT_NEW,
        nullable=False,
        new_name="provider_adjustment_id",
    )
    op.add_column(
        "billing_refunds",
        sa.Column("provider", sa.String(32), nullable=True, comment=_REFUND_PROVIDER),
    )
    op.execute("UPDATE billing_refunds SET provider = 'paddle'")
    op.alter_column(
        "billing_refunds",
        "provider",
        existing_type=sa.String(32),
        nullable=False,
        existing_comment=_REFUND_PROVIDER,
    )
    op.drop_constraint(
        "uq_billing_refunds_paddle_adjustment", "billing_refunds", type_="unique"
    )
    op.create_unique_constraint(
        "uq_billing_refunds_provider_adjustment",
        "billing_refunds",
        ["provider", "provider_adjustment_id"],
    )
    op.create_check_constraint(
        "ck_billing_refunds_provider",
        "billing_refunds",
        f"provider ~ {_PROVIDER_NAME}",
    )
    _comment("billing_refunds", "state", sa.String(16), _STATE_NEW, nullable=False)
    _comment(
        "billing_refunds",
        "decided_at",
        sa.DateTime(timezone=True),
        _DECIDED_NEW,
        nullable=True,
    )
    op.create_table_comment(
        "billing_refunds", _REFUNDS_NEW, existing_comment=_REFUNDS_OLD
    )


def _refunds_down() -> None:
    op.drop_constraint("ck_billing_refunds_provider", "billing_refunds", type_="check")
    op.drop_constraint(
        "uq_billing_refunds_provider_adjustment", "billing_refunds", type_="unique"
    )
    op.drop_column("billing_refunds", "provider")
    _comment(
        "billing_refunds",
        "provider_adjustment_id",
        sa.String(64),
        _ADJUSTMENT_OLD,
        nullable=False,
        new_name="paddle_adjustment_id",
    )
    op.create_unique_constraint(
        "uq_billing_refunds_paddle_adjustment",
        "billing_refunds",
        ["paddle_adjustment_id"],
    )
    _comment("billing_refunds", "state", sa.String(16), _STATE_OLD, nullable=False)
    _comment(
        "billing_refunds",
        "decided_at",
        sa.DateTime(timezone=True),
        _DECIDED_OLD,
        nullable=True,
    )
    op.create_table_comment(
        "billing_refunds", _REFUNDS_OLD, existing_comment=_REFUNDS_NEW
    )


def _events() -> None:
    op.create_table(
        "billing_provider_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "provider",
            sa.String(32),
            nullable=False,
            comment="The provider's adapter name (BILLING_PROVIDERS), e.g. paddle.",
        ),
        sa.Column(
            "provider_event_id",
            sa.String(64),
            nullable=False,
            comment="The provider's id of the event; unique per provider, so a "
            "repeat is recognised.",
        ),
        sa.Column(
            "event_type",
            sa.String(32),
            nullable=False,
            comment="purchase_completed | refund_approved | refund_rejected | "
            "chargeback | unknown: the event as billing reads it.",
        ),
        sa.Column(
            "provider_event_type",
            sa.String(64),
            nullable=False,
            comment="The provider's own name of the event type.",
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            comment="processed (the journal acted) | ignored (not an event "
            "billing acts on) | recorded (a chargeback: kept, no action) | "
            "failed (see error; handled again when the provider repeats the "
            "event).",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Why the handling failed, without personal data; NULL unless "
            "failed.",
        ),
        sa.Column(
            "body_sha256",
            sa.String(64),
            nullable=False,
            comment="SHA-256 (hex) of the raw body last handled; the body itself "
            "is not kept.",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the event first arrived.",
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the event was last handled.",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it happened at the provider, by the provider's word.",
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            nullable=True,
            comment="The account the event's custom data names. No FK: it may "
            "name none, and the event then failed.",
        ),
        sa.Column(
            "package_id",
            sa.String(64),
            nullable=True,
            comment="Purchase: the package bought (config/billing_packages.yaml).",
        ),
        sa.Column(
            "amount_minor",
            sa.BigInteger(),
            nullable=True,
            comment="Purchase: the amount paid, tax included; refund, chargeback: "
            "the amount. In minor units of currency.",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=True,
            comment="ISO 4217 code of amount_minor.",
        ),
        sa.Column(
            "provider_transaction_id",
            sa.String(64),
            nullable=True,
            comment="The provider's transaction id.",
        ),
        sa.Column(
            "provider_line_id",
            sa.String(64),
            nullable=True,
            comment="Purchase: the provider's id of the line within the transaction.",
        ),
        sa.Column(
            "provider_adjustment_id",
            sa.String(64),
            nullable=True,
            comment="Refund: the provider's adjustment id.",
        ),
        sa.CheckConstraint(
            f"provider ~ {_PROVIDER_NAME}",
            name="ck_billing_provider_events_provider",
        ),
        sa.CheckConstraint(
            "event_type IN ('purchase_completed', 'refund_approved', "
            "'refund_rejected', 'chargeback', 'unknown')",
            name="ck_billing_provider_events_event_type",
        ),
        sa.CheckConstraint(
            "state IN ('processed', 'ignored', 'recorded', 'failed')",
            name="ck_billing_provider_events_state",
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (error IS NOT NULL)",
            name="ck_billing_provider_events_error",
        ),
        sa.CheckConstraint(
            "body_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_billing_provider_events_body_sha256",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_billing_provider_events_currency",
        ),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uq_billing_provider_events_event"
        ),
        comment="Webhook events of payment providers, one row per event "
        "(BIL-PAY): the normalized fields and how the event was handled; never "
        "the raw body or personal data.",
    )


def _audit_action_comment(comment: str) -> None:
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=comment,
    )


def _refuse_other_providers() -> None:
    """Stop the downgrade while a batch or refund is not Paddle's (docstring)."""
    others = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT (SELECT count(*) FROM billing_batches "
                "WHERE provider <> 'paddle') + (SELECT count(*) FROM "
                "billing_refunds WHERE provider <> 'paddle')"
            )
        )
        .scalar_one()
    )
    if others:
        msg = (
            f"{others} batch(es) or refund(s) belong to a provider other than "
            "Paddle (an invoice, a test provider); below this revision they "
            "would read as Paddle purchases. Settle them by hand first."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    """Rename to provider-neutral columns, key purchases and refunds by provider,
    create the table of provider events."""
    _batches_up()
    _refunds_up()
    _events()
    _audit_action_comment(_AUDIT_ACTION_NEW)


def downgrade() -> None:
    """Refused while a non-Paddle batch or refund exists; else the reverse."""
    _refuse_other_providers()
    _audit_action_comment(_AUDIT_ACTION_OLD)
    op.drop_table("billing_provider_events")
    _refunds_down()
    _batches_down()
