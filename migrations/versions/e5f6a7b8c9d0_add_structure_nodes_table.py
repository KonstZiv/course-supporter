"""Add structure_nodes table (S3-015).

Recursive adjacency list for generated course structure.
Each node belongs to a StructureSnapshot and forms a tree
via parent_structurenode_id self-reference.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-05 20:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "structure_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "structuresnapshot_id",
            sa.Uuid(),
            sa.ForeignKey("structure_snapshots.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "parent_structurenode_id",
            sa.Uuid(),
            sa.ForeignKey("structure_nodes.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("node_type", sa.String(30), nullable=False, index=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        # Section 1: Formal & organisational
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("learning_goal", sa.Text(), nullable=True),
        sa.Column(
            "expected_knowledge", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "expected_skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "prerequisites", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("difficulty", sa.String(20), nullable=True),
        sa.Column("estimated_duration", sa.Integer(), nullable=True),
        # Section 2: Results & assessment
        sa.Column("success_criteria", sa.Text(), nullable=True),
        sa.Column("assessment_method", sa.String(50), nullable=True),
        sa.Column(
            "competencies", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        # Section 3: Methodological accents
        sa.Column(
            "key_concepts", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "common_mistakes", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("teaching_strategy", sa.String(50), nullable=True),
        sa.Column("activities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Section 4: Context & adaptivity
        sa.Column("teaching_style", sa.String(50), nullable=True),
        sa.Column(
            "deep_dive_references",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("content_version", sa.DateTime(timezone=True), nullable=True),
        # Section 5: Material references
        sa.Column("timecodes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "slide_references", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "web_references", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("structure_nodes")
