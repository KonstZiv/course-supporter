"""mentor-rebuild task 08, commit K3: ``claimed_at`` is the claim's heartbeat.

Revision ID: criteria_claim_heartbeat
Revises: criteria_list_tables
Create Date: 2026-09-30

A claimer of a criteria list now renews ``claimed_at`` while it composes, and a
claim counts as abandoned once it has been silent for longer than the service's
threshold (task 08, section 9, decision 2 as refined on 2026-09-30). The comment
of ``task_criteria_lists.claimed_at`` said "when the current claim started",
which is no longer what the column holds; this puts the ORM's comment in its
place.

A comment only: no column, constraint or row changes, so both directions are
lossless. Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on
this database (``DD-L4-A``). The ORM mirror is
``storage.orm.TaskCriteriaList.claimed_at``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "criteria_claim_heartbeat"
down_revision: str | Sequence[str] | None = "criteria_list_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE = (
    "When the current claim started; renewed when an abandoned claim is taken "
    "over. The takeover and the final write compare it, so a claimer that lost "
    "its claim cannot write over the new claimer's list."
)
_AFTER = (
    "The current claimer's last sign of life: set when the claim is made, "
    "renewed by the claimer's heartbeat while it composes and by a takeover. A "
    "claim silent for longer than the service's threshold is abandoned. The "
    "renewal, the takeover and the final write compare it, so a claimer that "
    "lost its claim cannot write over the new claimer's list."
)


def _comment(comment: str, existing: str) -> None:
    op.alter_column(
        "task_criteria_lists",
        "claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
        comment=comment,
        existing_comment=existing,
    )


def upgrade() -> None:
    """Say what the column holds now."""
    _comment(_AFTER, _BEFORE)


def downgrade() -> None:
    """Put the comment of commit K1 back."""
    _comment(_BEFORE, _AFTER)
