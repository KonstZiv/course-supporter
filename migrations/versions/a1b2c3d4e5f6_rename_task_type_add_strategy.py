"""Rename task_type to action, add strategy column to llm_calls.

Revision ID: a1b2c3d4e5f6
Revises: 6990e1d9124e
Create Date: 2026-02-13 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "6990e1d9124e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename task_type → action, add strategy column."""
    op.alter_column("llm_calls", "task_type", new_column_name="action")
    op.add_column(
        "llm_calls",
        sa.Column(
            "strategy",
            sa.String(length=50),
            server_default="default",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Reverse: drop strategy, rename action → task_type."""
    op.drop_column("llm_calls", "strategy")
    op.alter_column("llm_calls", "action", new_column_name="task_type")
