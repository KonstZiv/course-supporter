"""add summary_nested_nodes to structure_snapshots

Revision ID: 8ccb4561f29e
Revises: e7b30d6e6533
Create Date: 2026-03-11 14:44:32.342335

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "8ccb4561f29e"
down_revision: Union[str, Sequence[str], None] = "e7b30d6e6533"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add summary_nested_nodes for recursive context compression."""
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "summary_nested_nodes",
            sa.Text(),
            nullable=True,
            comment="LLM-generated compressed summary of all nested node snapshots",
        ),
    )


def downgrade() -> None:
    """Remove summary_nested_nodes column."""
    op.drop_column("structure_snapshots", "summary_nested_nodes")
