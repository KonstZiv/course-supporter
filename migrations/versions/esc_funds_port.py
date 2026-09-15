"""mentor-rebuild task 02: the call register keeps the funds port's numbers.

Revision ID: esc_funds_port
Revises: esc_call_outcome
Create Date: 2026-09-15

The funds port answers before a submission's first paid call; its first
implementation allows everything and only records numbers, so that the
estimate can later be calibrated against what was actually paid. Those numbers
live in the call register, on a row of their own that records no model call
(KD5, paragraph on rows without a model call):

* ``ceiling_estimate_usd`` — the path's ceiling estimate in dollars, the same
  type as ``cost_usd`` so the estimate and the actual cost add up directly;
* ``funds_decision`` — ``allowed`` / ``refused`` (``FundsDecision``),
  CHECK-bound;
* ``funds_refusal_reason`` — the refusal's reason code; a CHECK ties it to the
  decision: required on a refusal, absent otherwise;
* ``path_key`` — the path the estimate belongs to, in ``PathKey`` text form
  (``task/first``).

On that row ``provider``, ``model_id``, ``success`` and ``cost_usd`` stay NULL,
so cost sums (``cost_usd IS NOT NULL``) and the failure count skip it. The
comments of ``provider``, ``model_id`` and ``success`` name it next to the rows
without a call that task 01 introduced.

NO back-fill: every row written before this migration recorded no port answer,
and the new columns stay NULL on it.

The decision value list is spelled out literally on purpose: the ORM builds the
same list from the enum, and a migration must not change meaning when the code
later does. ``tests/integration/test_external_service_call_db.py`` writes every
enum member through the database.

DELIBERATELY NOT included: the whole-schema comment and index drift that
``alembic revision --autogenerate`` reports on this database (DD-L4-A). This
file is hand-written (SPRINT rule 6) and carries only the changes above.

Downgrade drops the four columns with their CHECKs and restores the task-01
comments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "esc_funds_port"
down_revision: str | Sequence[str] | None = "esc_call_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "external_service_calls"

_FUNDS_DECISIONS = ("allowed", "refused")

_FUNDS_ROW = (
    " Funds-port row only (mentor-rebuild task 02; KD5, rows without a model "
    "call): provider, model_id, success and cost_usd stay NULL on it, and cost "
    "sums skip it."
)

_NO_CALL_COMMENTS = {
    "provider": (
        sa.String(length=50),
        "Provider that was called. NULL = no call was made (the per-review "
        "metrics row).",
        "Provider that was called. NULL = no call was made (the per-review "
        "metrics and funds-port rows).",
    ),
    "model_id": (
        sa.String(length=100),
        "Model that was called. NULL = no call was made (the per-review metrics row).",
        "Model that was called. NULL = no call was made (the per-review "
        "metrics and funds-port rows).",
    ),
    "success": (
        sa.Boolean(),
        "Transport result only: did the call return a response. NULL = no call "
        "was made (ladder trace and metrics rows). Whether the response was "
        "usable is outcome.",
        "Transport result only: did the call return a response. NULL = no call "
        "was made (ladder trace, per-review metrics and funds-port rows). "
        "Whether the response was usable is outcome.",
    ),
}


def upgrade() -> None:
    """Add the funds-port columns and their CHECKs."""
    op.add_column(
        _TABLE,
        sa.Column(
            "ceiling_estimate_usd",
            sa.Float(),
            nullable=True,
            comment="The path's ceiling estimate in dollars: the sum of its "
            "stages' money ceilings, the same type as cost_usd." + _FUNDS_ROW,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "funds_decision",
            sa.String(length=16),
            nullable=True,
            comment="The funds port's answer before the first paid call "
            "(FundsDecision): allowed / refused." + _FUNDS_ROW,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "funds_refusal_reason",
            sa.String(length=64),
            nullable=True,
            comment="Reason code of a refusal; set exactly when funds_decision = "
            "'refused'." + _FUNDS_ROW,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "path_key",
            sa.String(length=64),
            nullable=True,
            comment="The submission path the estimate belongs to, as "
            "'<task type>/<submission state>' (PathKey text, e.g. 'task/first')."
            + _FUNDS_ROW,
        ),
    )

    listed = ", ".join(f"'{value}'" for value in _FUNDS_DECISIONS)
    op.create_check_constraint(
        "ck_esc_funds_decision",
        _TABLE,
        f"funds_decision IS NULL OR funds_decision IN ({listed})",
    )
    # Both sides are never NULL, so the CHECK cannot pass by evaluating to NULL
    # (a reason on a row with no decision would otherwise slip through).
    op.create_check_constraint(
        "ck_esc_funds_refusal_reason",
        _TABLE,
        "(funds_decision IS NOT DISTINCT FROM 'refused') "
        "= (funds_refusal_reason IS NOT NULL)",
    )

    for column, (type_, old_comment, new_comment) in _NO_CALL_COMMENTS.items():
        op.alter_column(
            _TABLE,
            column,
            existing_type=type_,
            existing_nullable=True,
            comment=new_comment,
            existing_comment=old_comment,
        )


def downgrade() -> None:
    """Drop the funds-port columns and restore the task-01 comments."""
    for column, (type_, old_comment, new_comment) in _NO_CALL_COMMENTS.items():
        op.alter_column(
            _TABLE,
            column,
            existing_type=type_,
            existing_nullable=True,
            comment=old_comment,
            existing_comment=new_comment,
        )

    op.drop_constraint("ck_esc_funds_refusal_reason", _TABLE, type_="check")
    op.drop_constraint("ck_esc_funds_decision", _TABLE, type_="check")

    for column in (
        "path_key",
        "funds_refusal_reason",
        "funds_decision",
        "ceiling_estimate_usd",
    ):
        op.drop_column(_TABLE, column)
