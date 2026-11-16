"""Mentor: the reason a verdict was asked about once more, kept on its row.

Revision ID: verdict_repeat_reason
Revises: bile_charging_order_hold
Create Date: 2026-11-16

The evaluation stage asks the model once more about the verdicts that cannot
stand as given, each with its reason (``RepeatReason``); the reason reached a
log line alone, and the row kept only ``retried``. Repeats are about a
quarter of the stage's cost, so the product owner decided the reason is kept
beside the verdict — the share and the cost of repeats are then counted from
the database (with the stage's rows in ``external_service_calls``) by reason,
item, list and model, without the containers' logs.

Adds to ``submission_criterion_verdicts``:

* ``repeat_reason`` — ``VARCHAR(32)``, nullable: the reason the repeated
  request named (``homework/criteria_verdicts.py::_repeat_reason`` on the
  model's first answer). NULL when the item was not asked again.
* ``ck_submission_criterion_verdicts_repeat_reason`` — a value of
  ``RepeatReason`` or NULL, and a reason only on a retried row. Not the
  converse: a row retried before this revision has no reason.

The vocabulary is spelled out LITERALLY here, as in
``submission_verdict_tables``: the ORM builds the CHECK from
``criteria_kinds.RepeatReason``, and a migration must not change meaning when
the code later does. The column comment is copied from the ORM
(``storage.orm.SubmissionCriterionVerdict``); a test compares the live one
with it. No existing row is rewritten: rows written before this revision keep
NULL, even when ``retried`` is true.

Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: drops the CHECK and the column — and with them every reason
written since this revision; that loss is not recoverable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "verdict_repeat_reason"
down_revision: str | Sequence[str] | None = "bile_charging_order_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERDICTS = "submission_criterion_verdicts"
_CHECK = "ck_submission_criterion_verdicts_repeat_reason"

# Spelled out, not derived from the enum: see the module docstring.
_REASONS = (
    "'no_quote', 'quote_not_one_line', 'quote_too_long', 'quote_too_short', "
    "'quote_not_found', 'quote_from_base', 'no_sentence'"
)

_COMMENT = (
    "Why the item was asked about once more (RepeatReason), as the repeated "
    "request named it: homework/criteria_verdicts.py::_repeat_reason on the "
    "model's first answer. NULL when the item was not asked again, and on rows "
    "written before revision verdict_repeat_reason even when retried is true."
)


def upgrade() -> None:
    """Add the column and its CHECK; existing rows keep NULL."""
    op.add_column(
        _VERDICTS,
        sa.Column(
            "repeat_reason", sa.String(length=32), nullable=True, comment=_COMMENT
        ),
    )
    op.create_check_constraint(
        _CHECK,
        _VERDICTS,
        f"(repeat_reason IS NULL OR repeat_reason IN ({_REASONS})) "
        "AND (repeat_reason IS NULL OR retried)",
    )


def downgrade() -> None:
    """Drop the CHECK and the column — the reasons written are lost."""
    op.drop_constraint(_CHECK, _VERDICTS, type_="check")
    op.drop_column(_VERDICTS, "repeat_reason")
