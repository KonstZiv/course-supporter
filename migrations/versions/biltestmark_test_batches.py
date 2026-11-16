"""BIL-TESTMARK: batches marked as test purchases; their units voided.

Revision ID: biltestmark_test_batches
Revises: bile_charging_order_hold
Create Date: 2026-11-16

While the payment provider (Creem) is in its test mode a test card pays and a
real batch of units is credited; nothing in the journal told it apart.
Operator 2026-11-16: test purchases are marked in the database and their units
are taken out by one administrator's command before charging is switched on.

* ``billing_test_batches`` — new, one row per marked batch: ``batch_id``
  (primary key, a foreign key to ``billing_batches`` without ON DELETE, as
  every key inside billing), ``marked_at``, ``marked_by`` (``credit`` |
  ``backfill``). A table, not a column of ``billing_batches``: a batch is
  insert-only and cannot be updated without going round its trigger. It gets
  the same insert-only trigger (``billing_refuse_update``, BIL-A): UPDATE is
  refused; DELETE stays allowed, for the five-year destruction
  (``billing.retention``), which deletes the marks before the batches.
* ``billing_movements`` — a new kind ``test_voided`` (free units of a marked
  batch taken out): in ``ck_billing_movements_kind`` and among the kinds with
  a batch (``ck_billing_movements_batch``); the column comment of ``kind``.
* ``billing_audit_log.action`` — comment only: gains ``batch.test_void`` and
  ``account.create`` (the latter added without a migration by BIL-E3).
  ``billing_audit_log.actor`` — comment only: an ``account.create`` entry may
  name ``system`` instead of an administrator (BIL-E3).
  ``entity`` already names ``account`` and ``batch``: unchanged.
* The backfill: every batch already there with ``source = 'purchase'`` and
  ``provider = 'creem'`` is marked ``backfill``. The ground (the operator's
  fact): production never had Creem's live keys, so every Creem purchase made
  so far was a test one (on production one, 2026-11-14, the service tenant
  smoke-test). A contract invoice (``provider = 'invoice'``) and an
  administrator's credit (``source = 'admin_credit'``) are not marked.

Column and table comments are copied from the ORM (``storage.orm.
BillingTestBatch``, ``BillingMovement``, ``BillingAuditEntry``). Hand-written;
``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: refused while a mark or a ``test_voided`` movement exists (the
schema below has no place for either). Otherwise it drops the table (its
trigger goes with it) and restores the checks and the comments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "biltestmark_test_batches"
down_revision: str | Sequence[str] | None = "bile_charging_order_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSERT_ONLY_FUNCTION = "billing_refuse_update"  # created by bila_billing_accounts
_TABLE = "billing_test_batches"

_MOVEMENT_KINDS_OLD = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_arisen', "
    "'difference_repaid', 'batch_expired', 'difference_annulled', "
    "'benefit_covered', 'platform_covered', 'refund_hold', 'refund_completed', "
    "'refund_rejected')"
)
_MOVEMENT_KINDS_NEW = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_arisen', "
    "'difference_repaid', 'batch_expired', 'difference_annulled', "
    "'benefit_covered', 'platform_covered', 'refund_hold', 'refund_completed', "
    "'refund_rejected', 'test_voided')"
)
_MOVEMENTS_WITH_BATCH_OLD = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_repaid', "
    "'batch_expired', 'refund_hold', 'refund_completed', 'refund_rejected')"
)
_MOVEMENTS_WITH_BATCH_NEW = (
    "('credit', 'reserve', 'reserve_release', 'debit', 'difference_repaid', "
    "'batch_expired', 'refund_hold', 'refund_completed', 'refund_rejected', "
    "'test_voided')"
)

_KIND_COMMENT_OLD = (
    "credit | reserve | reserve_release | debit | difference_arisen "
    "| difference_repaid | batch_expired | difference_annulled | "
    "benefit_covered | platform_covered | refund_hold | refund_completed | "
    "refund_rejected."
)
_KIND_COMMENT_NEW = (
    "credit | reserve | reserve_release | debit | difference_arisen "
    "| difference_repaid | batch_expired | difference_annulled | "
    "benefit_covered | platform_covered | refund_hold | refund_completed | "
    "refund_rejected | test_voided."
)
_ACTION_OLD = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, account.test_purchase, "
    "batch.admin_credit, batch.invoice_credit, dispute.lost, dispute.won, "
    "refund.order_hold, refund.order_release."
)
_ACTION_NEW = (
    "What was done, e.g. author_settings.update, payment_rule.create, "
    "payment_rule.end, account.anonymize, account.test_purchase, "
    "batch.admin_credit, batch.invoice_credit, dispute.lost, dispute.won, "
    "refund.order_hold, refund.order_release, batch.test_void, "
    "account.create."
)

_ACTOR_OLD = "Who made the change (the administrator's --actor)."
_ACTOR_NEW = (
    "Who made the change: the administrator's --actor, or system for an "
    "account created by a user's or the system's action (account.create)."
)


def _movement_checks(kinds: str, with_batch: str) -> None:
    op.drop_constraint("ck_billing_movements_kind", "billing_movements", type_="check")
    op.create_check_constraint(
        "ck_billing_movements_kind", "billing_movements", f"kind IN {kinds}"
    )
    op.drop_constraint("ck_billing_movements_batch", "billing_movements", type_="check")
    op.create_check_constraint(
        "ck_billing_movements_batch",
        "billing_movements",
        f"(kind IN {with_batch}) = (batch_id IS NOT NULL)",
    )


def _actor_comment(comment: str, existing: str) -> None:
    op.alter_column(
        "billing_audit_log",
        "actor",
        existing_type=sa.String(200),
        existing_nullable=False,
        comment=comment,
        existing_comment=existing,
    )


def _comments(kind: str, kind_old: str, action: str, action_old: str) -> None:
    op.alter_column(
        "billing_movements",
        "kind",
        existing_type=sa.String(32),
        existing_nullable=False,
        comment=kind,
        existing_comment=kind_old,
    )
    op.alter_column(
        "billing_audit_log",
        "action",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=action,
        existing_comment=action_old,
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("billing_batches.id"),
            primary_key=True,
            comment="The batch marked; one mark per batch.",
        ),
        sa.Column(
            "marked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the mark was written: the credit's time, or the migration's.",
        ),
        sa.Column(
            "marked_by",
            sa.String(16),
            nullable=False,
            comment="credit (marked with the credit: the provider was in its test "
            "mode) | backfill (a Creem purchase marked by the migration that "
            "brought the marks).",
        ),
        sa.CheckConstraint(
            "marked_by IN ('credit', 'backfill')",
            name="ck_billing_test_batches_marked_by",
        ),
        comment="Batches marked as test purchases (BIL-TESTMARK): paid in "
        "a payment provider's test mode. Insert-only.",
    )
    # The table's name is the constant above, never input.
    op.execute(
        f"CREATE TRIGGER {_TABLE}_insert_only BEFORE UPDATE ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {_INSERT_ONLY_FUNCTION}();"
    )
    _movement_checks(_MOVEMENT_KINDS_NEW, _MOVEMENTS_WITH_BATCH_NEW)
    _comments(_KIND_COMMENT_NEW, _KIND_COMMENT_OLD, _ACTION_NEW, _ACTION_OLD)
    _actor_comment(_ACTOR_NEW, _ACTOR_OLD)
    # The backfill (module docstring): every Creem purchase so far was a test.
    op.execute(
        "INSERT INTO billing_test_batches (batch_id, marked_at, marked_by) "
        "SELECT id, now(), 'backfill' FROM billing_batches "
        "WHERE source = 'purchase' AND provider = 'creem'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    marks = bind.execute(
        sa.text("SELECT count(*) FROM billing_test_batches")
    ).scalar_one()
    voided = bind.execute(
        sa.text("SELECT count(*) FROM billing_movements WHERE kind = 'test_voided'")
    ).scalar_one()
    if marks or voided:
        raise RuntimeError(
            f"{marks} test-purchase mark(s) and {voided} test_voided movement(s) "
            "exist; the schema below biltestmark_test_batches has no place for "
            "them — refusing to downgrade"
        )
    _actor_comment(_ACTOR_OLD, _ACTOR_NEW)
    _comments(_KIND_COMMENT_OLD, _KIND_COMMENT_NEW, _ACTION_OLD, _ACTION_NEW)
    _movement_checks(_MOVEMENT_KINDS_OLD, _MOVEMENTS_WITH_BATCH_OLD)
    op.drop_table(_TABLE)
