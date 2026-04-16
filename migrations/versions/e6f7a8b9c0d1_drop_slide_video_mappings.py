"""drop slide_video_mappings table (feature never used on prod)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-16 09:00:00.000000

Drops the ``slide_video_mappings`` table and its
``mapping_validation_state_enum`` Postgres type. The feature was never
used in production (confirmed via audit 2026-04-15); downgrade re-creates
the schema for symmetrical rollback but rows cannot be restored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop slide_video_mappings table and its enum type."""
    op.drop_index(
        op.f("ix_slide_video_mappings_video_materialentry_id"),
        table_name="slide_video_mappings",
    )
    op.drop_index(
        op.f("ix_slide_video_mappings_presentation_materialentry_id"),
        table_name="slide_video_mappings",
    )
    op.drop_index(
        op.f("ix_slide_video_mappings_materialnode_id"),
        table_name="slide_video_mappings",
    )
    op.drop_table("slide_video_mappings")
    op.execute("DROP TYPE IF EXISTS mapping_validation_state_enum")


def downgrade() -> None:
    """Re-create slide_video_mappings table and its enum (schema only).

    Data cannot be restored — prior upgrade dropped the table.
    """
    op.create_table(
        "slide_video_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "materialnode_id",
            sa.Uuid(),
            nullable=False,
            comment="FK to owning MaterialNode",
        ),
        sa.Column("presentation_materialentry_id", sa.Uuid(), nullable=False),
        sa.Column("video_materialentry_id", sa.Uuid(), nullable=False),
        sa.Column("slide_number", sa.Integer(), nullable=False),
        sa.Column("video_timecode_start", sa.String(length=20), nullable=False),
        sa.Column("video_timecode_end", sa.String(length=20), nullable=True),
        sa.Column(
            "validation_state",
            sa.Enum(
                "validated",
                "pending_validation",
                "validation_failed",
                name="mapping_validation_state_enum",
            ),
            nullable=False,
            server_default=sa.text("'pending_validation'"),
        ),
        sa.Column(
            "blocking_factors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="JSONB array of reasons preventing validation",
        ),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["materialnode_id"], ["material_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["presentation_materialentry_id"],
            ["material_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_materialentry_id"],
            ["material_entries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Presentation slide to video timecode mappings",
    )
    op.create_index(
        op.f("ix_slide_video_mappings_materialnode_id"),
        "slide_video_mappings",
        ["materialnode_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_slide_video_mappings_presentation_materialentry_id"),
        "slide_video_mappings",
        ["presentation_materialentry_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_slide_video_mappings_video_materialentry_id"),
        "slide_video_mappings",
        ["video_materialentry_id"],
        unique=False,
    )
