"""add structure_nodes_editable table

Revision ID: a1f2e3d4b5c6
Revises: 8ccb4561f29e
Create Date: 2026-03-12 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a1f2e3d4b5c6"
down_revision: Union[str, Sequence[str], None] = "8ccb4561f29e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create structure_nodes_editable table."""
    op.create_table(
        "structure_nodes_editable",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "materialnode_id",
            sa.Uuid(),
            sa.ForeignKey("material_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_snapshot_id",
            sa.Uuid(),
            sa.ForeignKey("structure_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_structurenode_id",
            sa.Uuid(),
            sa.ForeignKey("structure_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "parent_editable_id",
            sa.Uuid(),
            sa.ForeignKey("structure_nodes_editable.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("node_type", sa.String(30), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        # Section 1: Formal & organisational
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("learning_goal", sa.Text(), nullable=True),
        sa.Column("expected_knowledge", JSONB(), nullable=True),
        sa.Column("expected_skills", JSONB(), nullable=True),
        sa.Column("prerequisites", JSONB(), nullable=True),
        sa.Column("difficulty", sa.String(20), nullable=True),
        sa.Column("estimated_duration", sa.Integer(), nullable=True),
        # Section 2: Results & assessment
        sa.Column("success_criteria", sa.Text(), nullable=True),
        sa.Column("assessment_method", sa.String(50), nullable=True),
        sa.Column("competencies", JSONB(), nullable=True),
        # Section 3: Methodological accents
        sa.Column("key_concepts", JSONB(), nullable=True),
        sa.Column("common_mistakes", JSONB(), nullable=True),
        sa.Column("teaching_strategy", sa.String(50), nullable=True),
        sa.Column("activities", JSONB(), nullable=True),
        # Section 4: Context & adaptivity
        sa.Column("teaching_style", sa.String(50), nullable=True),
        sa.Column("deep_dive_references", JSONB(), nullable=True),
        sa.Column("content_version", sa.DateTime(timezone=True), nullable=True),
        # Section 5: Material references
        sa.Column("timecodes", JSONB(), nullable=True),
        sa.Column("slide_references", JSONB(), nullable=True),
        sa.Column("web_references", JSONB(), nullable=True),
        # Edit tracking
        sa.Column("edited_fields", JSONB(), nullable=False, server_default="[]"),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        comment=(
            "Mutable overlay for course structure nodes. "
            "Working copy that survives re-generation"
        ),
    )

    op.create_index(
        "ix_structure_nodes_editable_materialnode_id",
        "structure_nodes_editable",
        ["materialnode_id"],
    )
    op.create_index(
        "ix_structure_nodes_editable_source_snapshot_id",
        "structure_nodes_editable",
        ["source_snapshot_id"],
    )
    op.create_index(
        "ix_structure_nodes_editable_source_structurenode_id",
        "structure_nodes_editable",
        ["source_structurenode_id"],
    )
    op.create_index(
        "ix_structure_nodes_editable_parent_editable_id",
        "structure_nodes_editable",
        ["parent_editable_id"],
    )
    op.create_index(
        "ix_structure_nodes_editable_node_type",
        "structure_nodes_editable",
        ["node_type"],
    )


def downgrade() -> None:
    """Drop structure_nodes_editable table."""
    op.drop_table("structure_nodes_editable")
