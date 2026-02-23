"""add course_structure_snapshots

Revision ID: b1c2d3e4f5a6
Revises: a8f1e2c3d4b5
Create Date: 2026-02-23 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a8f1e2c3d4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "course_structure_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("node_fingerprint", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("structure", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=True),
        sa.Column("model_id", sa.String(100), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["material_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_snapshots_course", "course_structure_snapshots", ["course_id"])
    op.create_index("ix_snapshots_node", "course_structure_snapshots", ["node_id"])

    # Unique index with COALESCE to treat NULL node_id as a sentinel value
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshots_identity "
        "ON course_structure_snapshots("
        "course_id, "
        "COALESCE(node_id, '00000000-0000-0000-0000-000000000000'::uuid), "
        "node_fingerprint, "
        "mode)"
    )


def downgrade() -> None:
    op.drop_index("uq_snapshots_identity", table_name="course_structure_snapshots")
    op.drop_index("ix_snapshots_node", table_name="course_structure_snapshots")
    op.drop_index("ix_snapshots_course", table_name="course_structure_snapshots")
    op.drop_table("course_structure_snapshots")
