"""add snapshot step fields

Revision ID: e7b30d6e6533
Revises: f7b8d706ad9c
Create Date: 2026-03-06 17:08:29.803785

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7b30d6e6533"
down_revision: Union[str, Sequence[str], None] = "f7b8d706ad9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add step-related columns to structure_snapshots."""
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "step_type",
            sa.String(length=20),
            nullable=True,
            comment="Step type: generate, reconcile, refine",
        ),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="LLM-generated summary for cross-node context",
        ),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "core_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Concepts covered in depth at this node",
        ),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "mentioned_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Concepts mentioned but not covered in depth",
        ),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "corrections",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Reconciliation corrections audit trail",
        ),
    )


def downgrade() -> None:
    """Remove step-related columns from structure_snapshots."""
    op.drop_column("structure_snapshots", "corrections")
    op.drop_column("structure_snapshots", "mentioned_concepts")
    op.drop_column("structure_snapshots", "core_concepts")
    op.drop_column("structure_snapshots", "summary")
    op.drop_column("structure_snapshots", "step_type")
