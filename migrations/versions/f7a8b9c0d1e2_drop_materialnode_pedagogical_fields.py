"""drop material_nodes.learning_goal, expected_knowledge, expected_skills

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-04-16 14:00:00.000000

Pedagogical metadata was historically stored on ``material_nodes`` but
the source of truth moved to ``structure_nodes_editable`` (populated
by ArchitectAgent). No production code reads these columns; API
response schemas already stopped exposing them in PR #383.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop three pedagogical columns from material_nodes."""
    op.drop_column("material_nodes", "expected_skills")
    op.drop_column("material_nodes", "expected_knowledge")
    op.drop_column("material_nodes", "learning_goal")


def downgrade() -> None:
    """Re-create the three columns (all nullable, no data restored)."""
    op.add_column(
        "material_nodes",
        sa.Column(
            "learning_goal",
            sa.Text(),
            nullable=True,
            comment="Pedagogical goal text for guided generation mode",
        ),
    )
    op.add_column(
        "material_nodes",
        sa.Column(
            "expected_knowledge",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "material_nodes",
        sa.Column(
            "expected_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
