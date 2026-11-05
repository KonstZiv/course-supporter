"""BIL-B: the journal of units — batches, movements, reservations, differences, refunds.

Revision ID: bilb_unit_ledger
Revises: task13_attempt_ledger
Create Date: 2026-11-05

Creates five tables:

* ``billing_batches`` — a package of units on an account: a purchase (Paddle)
  or an administrator's credit, valid six calendar months; insert-only;
* ``billing_refunds`` — a refund of unused units of a purchase batch, held
  while Paddle reviews it (one row per Paddle adjustment);
* ``billing_reservations`` — the units held for one action until it ends;
  the action's key ``(action_kind, action_key)`` is unique;
* ``billing_differences`` — what an action cost beyond the payer's available
  units; repaid first from units that become free, annulled after 6 months;
* ``billing_movements`` — the journal: every movement of units, insert-only.

The rule of BIL-A holds: no foreign key to a table outside billing (the
journal outlives the account's owner), the foreign keys inside billing have
no ON DELETE action. ``billing_batches`` and ``billing_movements`` get the
insert-only trigger of BIL-A (``billing_refuse_update``, created there and
left to it on downgrade).

Two column comments of ``billing_audit_log`` gain the new audited action
``batch.admin_credit`` and entity ``batch``.

Column and table comments are copied from the ORM (``storage.orm.Billing*``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).
New tables rewrite no row. The downgrade drops them; lossless ONLY on a
database where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bilb_unit_ledger"
down_revision: str | Sequence[str] | None = "task13_attempt_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSERT_ONLY_FUNCTION = "billing_refuse_update"  # created by bila_billing_accounts
_INSERT_ONLY_TABLES = ("billing_batches", "billing_movements")

_ACTION_KINDS = "('review', 'processing', 'task_preparation')"
_MOVEMENT_KINDS = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_arisen', "
    "'difference_repaid', 'batch_expired', 'difference_annulled', "
    "'benefit_covered', 'platform_covered', 'refund_hold', 'refund_completed', "
    "'refund_rejected')"
)
_MOVEMENTS_WITH_BATCH = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_repaid', "
    "'batch_expired', 'refund_hold', 'refund_completed', 'refund_rejected')"
)
_MOVEMENTS_WITH_DIFFERENCE = (
    "('difference_arisen', 'difference_repaid', 'difference_annulled')"
)
_MOVEMENTS_WITH_REFUND = "('refund_hold', 'refund_completed', 'refund_rejected')"
_MOVEMENTS_OF_AN_ACTION = (
    "('reserve', 'reserve_release', 'debit', 'difference_arisen', "
    "'benefit_covered', 'platform_covered')"
)
_MOVEMENTS_WITH_PAYER = (
    "('debit', 'difference_arisen', 'benefit_covered', 'platform_covered')"
)

_AUDIT_ACTION_COMMENT_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize."
)
_AUDIT_ACTION_COMMENT_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, batch.admin_credit."
)
_AUDIT_ENTITY_COMMENT_OLD = (
    "Kind of row changed: account | author_settings | payment_rule."
)
_AUDIT_ENTITY_COMMENT_NEW = (
    "Kind of row changed: account | author_settings | payment_rule | batch."
)


def _account_fk(comment: str, *, nullable: bool = False) -> sa.Column:
    name = "beneficiary_account_id" if nullable else "account_id"
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey("billing_accounts.id"),
        nullable=nullable,
        comment=comment,
    )


def _instant(name: str, comment: str, *, server_now: bool) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=not server_now,
        server_default=sa.func.now() if server_now else None,
        comment=comment,
    )


def _action_columns(*, nullable: bool, key_comment: str) -> list[sa.Column]:
    kind_comment = (
        "review | processing | task_preparation; NULL when no action."
        if nullable
        else "review | processing | task_preparation."
    )
    return [
        sa.Column(
            "action_kind", sa.String(32), nullable=nullable, comment=kind_comment
        ),
        sa.Column("action_key", sa.String(200), nullable=nullable, comment=key_comment),
    ]


def _batches() -> None:
    op.create_table(
        "billing_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _account_fk("The account the units arrived on."),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            comment="purchase (paid through Paddle, refundable) | admin_credit "
            "(granted by the administrator, never refunded).",
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            comment="Units the batch brought; never changes.",
        ),
        _instant(
            "created_at",
            "When the units arrived; the start of the validity.",
            server_now=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="created_at + 6 calendar months (clamped to the month's last "
            "day). Units still free then burn.",
        ),
        sa.Column(
            "amount_paid_minor",
            sa.BigInteger(),
            nullable=True,
            comment="Purchase: the amount paid, tax included, in minor units of "
            "currency (cents). NULL on an admin credit.",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=True,
            comment="Purchase: ISO 4217 code of the amount paid.",
        ),
        sa.Column(
            "paddle_transaction_id",
            sa.String(64),
            nullable=True,
            comment="Purchase: Paddle transaction id; set by the Paddle adapter.",
        ),
        sa.Column(
            "paddle_line_id",
            sa.String(64),
            nullable=True,
            comment="Purchase: Paddle line item id of the package within the "
            "transaction; set by the Paddle adapter.",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="Admin credit: why the units were granted.",
        ),
        sa.Column(
            "actor",
            sa.String(200),
            nullable=True,
            comment="Admin credit: who granted them (--actor).",
        ),
        sa.CheckConstraint(
            "source IN ('purchase', 'admin_credit')",
            name="ck_billing_batches_source",
        ),
        sa.CheckConstraint("units > 0", name="ck_billing_batches_units_positive"),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_billing_batches_expires_after"
        ),
        sa.CheckConstraint(
            "source <> 'purchase' OR (amount_paid_minor IS NOT NULL "
            "AND currency IS NOT NULL AND reason IS NULL AND actor IS NULL)",
            name="ck_billing_batches_purchase",
        ),
        sa.CheckConstraint(
            "source <> 'admin_credit' OR (reason IS NOT NULL AND actor IS NOT NULL "
            "AND amount_paid_minor IS NULL AND currency IS NULL "
            "AND paddle_transaction_id IS NULL)",
            name="ck_billing_batches_admin_credit",
        ),
        sa.CheckConstraint(
            "amount_paid_minor IS NULL OR amount_paid_minor >= 0",
            name="ck_billing_batches_amount_not_negative",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_billing_batches_currency",
        ),
        sa.CheckConstraint(
            "(paddle_transaction_id IS NULL) = (paddle_line_id IS NULL)",
            name="ck_billing_batches_paddle_ids",
        ),
        comment="A package of units on an account (BIL-B): a purchase or an "
        "administrator's credit, valid six months. Insert-only.",
    )
    op.create_index("ix_billing_batches_account_id", "billing_batches", ["account_id"])
    op.create_index("ix_billing_batches_expires_at", "billing_batches", ["expires_at"])
    op.create_index(
        "uq_billing_batches_paddle_line",
        "billing_batches",
        ["paddle_transaction_id", "paddle_line_id"],
        unique=True,
        postgresql_where=sa.text("paddle_transaction_id IS NOT NULL"),
    )


def _refunds() -> None:
    op.create_table(
        "billing_refunds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _account_fk("The account of the batch."),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("billing_batches.id"),
            nullable=False,
            comment="The purchase batch the units are refunded from.",
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            comment="Units refunded: free units of the batch when held.",
        ),
        sa.Column(
            "amount_minor",
            sa.BigInteger(),
            nullable=False,
            comment="Money back: floor(amount_paid_minor * units / batch units), "
            "in minor units of the batch's currency.",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            comment="The batch's currency (ISO 4217).",
        ),
        sa.Column(
            "paddle_adjustment_id",
            sa.String(64),
            nullable=False,
            comment="Paddle adjustment (refund) id.",
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="held",
            comment="held (units blocked while Paddle reviews) | approved (units "
            "gone) | rejected (units back in the batch).",
        ),
        _instant("created_at", "When the units were held.", server_now=True),
        _instant(
            "decided_at",
            "When Paddle's decision was recorded; NULL while held.",
            server_now=False,
        ),
        sa.CheckConstraint(
            "state IN ('held', 'approved', 'rejected')",
            name="ck_billing_refunds_state",
        ),
        sa.CheckConstraint("units > 0", name="ck_billing_refunds_units_positive"),
        sa.CheckConstraint(
            "amount_minor >= 0", name="ck_billing_refunds_amount_not_negative"
        ),
        sa.CheckConstraint(
            "(state = 'held') = (decided_at IS NULL)",
            name="ck_billing_refunds_decided_at",
        ),
        sa.UniqueConstraint(
            "paddle_adjustment_id", name="uq_billing_refunds_paddle_adjustment"
        ),
        comment="Refund of unused units of a purchase batch (BIL-B): held while "
        "Paddle reviews it, then approved or rejected.",
    )
    op.create_index("ix_billing_refunds_account_id", "billing_refunds", ["account_id"])
    op.create_index("ix_billing_refunds_batch_id", "billing_refunds", ["batch_id"])


def _reservations() -> None:
    op.create_table(
        "billing_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_action_columns(
            nullable=False,
            key_comment="The action's identity as action_cost_estimates keeps it: "
            "the submission id, the job id, or "
            "'<authored_document_id>:<version>:<kind>'.",
        ),
        _account_fk("The account that pays (the payer)."),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            comment="Units held: the least of what was asked and what was available.",
        ),
        sa.Column(
            "allocation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The units per batch, earliest expiry first: "
            '[{"batch_id": "<uuid>", "units": n}, ...].',
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="active",
            comment="active (held) | settled (the action ended; charged) | "
            "released (the action failed; nothing charged).",
        ),
        sa.Column(
            "fact_units",
            sa.Integer(),
            nullable=True,
            comment="The action's actual cost in units, as settled; NULL until then.",
        ),
        _instant(
            "created_at",
            "When the units were held (the action started).",
            server_now=True,
        ),
        _instant(
            "finished_at",
            "When the reservation was settled or released; NULL while active.",
            server_now=False,
        ),
        sa.CheckConstraint(
            f"action_kind IN {_ACTION_KINDS}",
            name="ck_billing_reservations_action_kind",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'settled', 'released')",
            name="ck_billing_reservations_state",
        ),
        sa.CheckConstraint("units >= 0", name="ck_billing_reservations_units"),
        sa.CheckConstraint(
            "(state = 'active') = (finished_at IS NULL)",
            name="ck_billing_reservations_finished_at",
        ),
        sa.CheckConstraint(
            "(state = 'settled') = (fact_units IS NOT NULL)",
            name="ck_billing_reservations_fact_when_settled",
        ),
        sa.CheckConstraint(
            "fact_units IS NULL OR fact_units >= 0",
            name="ck_billing_reservations_fact_not_negative",
        ),
        sa.UniqueConstraint(
            "action_kind", "action_key", name="uq_billing_reservations_action"
        ),
        comment="Units held for one action until it ends (BIL-B); one row per "
        "action, the action's key unique.",
    )
    op.create_index(
        "ix_billing_reservations_account_id", "billing_reservations", ["account_id"]
    )


def _differences() -> None:
    op.create_table(
        "billing_differences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _account_fk("The payer the difference is recorded against."),
        *_action_columns(
            nullable=False,
            key_comment="The action whose cost went beyond the available units.",
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            comment="The shortfall when the difference arose.",
        ),
        sa.Column(
            "outstanding_units",
            sa.Integer(),
            nullable=False,
            comment="Units still to repay; 0 once repaid or annulled.",
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="open",
            comment="open | repaid | annulled.",
        ),
        _instant("created_at", "When the difference arose.", server_now=True),
        _instant(
            "annul_after",
            "created_at + 6 calendar months; NULL (never) for an author account "
            "allowed credit when the difference arose.",
            server_now=False,
        ),
        _instant(
            "closed_at",
            "When it was repaid in full or annulled; NULL while open.",
            server_now=False,
        ),
        sa.CheckConstraint(
            f"action_kind IN {_ACTION_KINDS}",
            name="ck_billing_differences_action_kind",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'repaid', 'annulled')",
            name="ck_billing_differences_state",
        ),
        sa.CheckConstraint("units > 0", name="ck_billing_differences_units_positive"),
        sa.CheckConstraint(
            "outstanding_units >= 0 AND outstanding_units <= units",
            name="ck_billing_differences_outstanding",
        ),
        sa.CheckConstraint(
            "(state = 'open') = (outstanding_units > 0)",
            name="ck_billing_differences_open_is_outstanding",
        ),
        sa.CheckConstraint(
            "(state = 'open') = (closed_at IS NULL)",
            name="ck_billing_differences_closed_at",
        ),
        comment="Units an action cost beyond what the payer had (BIL-B): repaid "
        "first from units that become free, annulled after 6 months.",
    )
    op.create_index(
        "ix_billing_differences_account_id", "billing_differences", ["account_id"]
    )


def _movements() -> None:
    op.create_table(
        "billing_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _account_fk("The account whose units moved (the payer of a charge)."),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("billing_batches.id"),
            nullable=True,
            comment="The batch the units moved in; NULL for kinds that touch none.",
        ),
        sa.Column(
            "difference_id",
            sa.Uuid(),
            sa.ForeignKey("billing_differences.id"),
            nullable=True,
            comment="The difference that arose, was repaid or was annulled.",
        ),
        sa.Column(
            "refund_id",
            sa.Uuid(),
            sa.ForeignKey("billing_refunds.id"),
            nullable=True,
            comment="The refund the units were held, refunded or returned for.",
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment="credit | reserve | reserve_release | debit | "
            "difference_arisen | difference_repaid | batch_expired | "
            "difference_annulled | benefit_covered | platform_covered | "
            "refund_hold | refund_completed | refund_rejected.",
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            comment="How many units; always positive, the kind says which way.",
        ),
        *_action_columns(
            nullable=True,
            key_comment="The action's key, as in action_cost_estimates and "
            "billing_reservations.",
        ),
        _account_fk('The account the action was for ("за кого").', nullable=True),
        sa.Column(
            "payer_source",
            sa.String(16),
            nullable=True,
            comment="Why this account paid: default | course_rule | student_rule "
            "| benefit (the author's benefit covered it) | platform "
            "(re-processing at the platform's initiative).",
        ),
        sa.Column(
            "rule_id",
            sa.Uuid(),
            sa.ForeignKey("billing_payment_rules.id"),
            nullable=True,
            comment="The payment rule applied (course_rule / student_rule).",
        ),
        sa.Column(
            "actor",
            sa.String(200),
            nullable=True,
            comment="Who made an administrative movement (--actor).",
        ),
        _instant("created_at", "When the units moved.", server_now=True),
        sa.CheckConstraint(
            f"kind IN {_MOVEMENT_KINDS}", name="ck_billing_movements_kind"
        ),
        sa.CheckConstraint("units > 0", name="ck_billing_movements_units_positive"),
        sa.CheckConstraint(
            f"(kind IN {_MOVEMENTS_WITH_BATCH}) = (batch_id IS NOT NULL)",
            name="ck_billing_movements_batch",
        ),
        sa.CheckConstraint(
            f"(kind IN {_MOVEMENTS_WITH_DIFFERENCE}) = (difference_id IS NOT NULL)",
            name="ck_billing_movements_difference",
        ),
        sa.CheckConstraint(
            f"(kind IN {_MOVEMENTS_WITH_REFUND}) = (refund_id IS NOT NULL)",
            name="ck_billing_movements_refund",
        ),
        sa.CheckConstraint(
            "(action_kind IS NULL) = (action_key IS NULL)",
            name="ck_billing_movements_action_pair",
        ),
        sa.CheckConstraint(
            f"action_kind IS NULL OR action_kind IN {_ACTION_KINDS}",
            name="ck_billing_movements_action_kind",
        ),
        sa.CheckConstraint(
            f"kind NOT IN {_MOVEMENTS_OF_AN_ACTION} OR action_key IS NOT NULL",
            name="ck_billing_movements_action_needed",
        ),
        sa.CheckConstraint(
            "payer_source IS NULL OR payer_source IN "
            "('default', 'course_rule', 'student_rule', 'benefit', 'platform')",
            name="ck_billing_movements_payer_source",
        ),
        sa.CheckConstraint(
            f"kind NOT IN {_MOVEMENTS_WITH_PAYER} OR (payer_source IS NOT NULL "
            "AND beneficiary_account_id IS NOT NULL)",
            name="ck_billing_movements_payer_needed",
        ),
        comment="Journal of unit movements on billing accounts (BIL-B): who, "
        "what, how many, for which action, for whom. Insert-only.",
    )
    op.create_index(
        "ix_billing_movements_account_created",
        "billing_movements",
        ["account_id", "created_at"],
    )
    op.create_index(
        "ix_billing_movements_action",
        "billing_movements",
        ["action_kind", "action_key"],
    )
    for column in ("batch_id", "difference_id", "refund_id", "beneficiary_account_id"):
        op.create_index(f"ix_billing_movements_{column}", "billing_movements", [column])
    op.create_index("ix_billing_movements_rule_id", "billing_movements", ["rule_id"])


def _insert_only() -> None:
    # Table names come from the hardcoded tuple above, never from input.
    for table in _INSERT_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_INSERT_ONLY_FUNCTION}();"
        )


def _audit_comments(action: str, entity: str) -> None:
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=action,
    )
    op.alter_column(
        "billing_audit_log",
        "entity",
        existing_type=sa.String(32),
        existing_nullable=False,
        comment=entity,
    )


def upgrade() -> None:
    """Create the five tables, their checks and indexes, the insert-only guard."""
    _batches()
    _refunds()
    _reservations()
    _differences()
    _movements()
    _insert_only()
    _audit_comments(_AUDIT_ACTION_COMMENT_NEW, _AUDIT_ENTITY_COMMENT_NEW)


def downgrade() -> None:
    """Drop the tables (indexes, constraints and triggers go with them)."""
    _audit_comments(_AUDIT_ACTION_COMMENT_OLD, _AUDIT_ENTITY_COMMENT_OLD)
    for table in (
        "billing_movements",
        "billing_differences",
        "billing_reservations",
        "billing_refunds",
        "billing_batches",
    ):
        op.drop_table(table)
