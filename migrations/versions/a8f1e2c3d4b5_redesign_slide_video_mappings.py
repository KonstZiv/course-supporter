"""redesign_slide_video_mappings

Revision ID: a8f1e2c3d4b5
Revises: 5129ac60d408
Create Date: 2026-02-21 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a8f1e2c3d4b5"
down_revision: Union[str, None] = "5129ac60d408"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop old table (no data migration — old schema is incompatible)
    op.drop_table("slide_video_mappings")

    # Create enum type via raw SQL to avoid ORM metadata conflict
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE mapping_validation_state_enum "
        "AS ENUM ('validated', 'pending_validation', 'validation_failed'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )

    # Create redesigned table (use sa.String to avoid SQLAlchemy auto-creating enum)
    op.create_table(
        "slide_video_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("presentation_entry_id", sa.Uuid(), nullable=False),
        sa.Column("video_entry_id", sa.Uuid(), nullable=False),
        sa.Column("slide_number", sa.Integer(), nullable=False),
        sa.Column("video_timecode_start", sa.String(20), nullable=False),
        sa.Column("video_timecode_end", sa.String(20), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "validation_state",
            sa.String(30),
            nullable=False,
            server_default="pending_validation",
        ),
        sa.Column("blocking_factors", postgresql.JSONB(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        sa.Column(
            "validated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["material_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["presentation_entry_id"],
            ["material_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_entry_id"],
            ["material_entries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Cast column to actual enum type (drop default first, re-add after)
    op.execute(
        "ALTER TABLE slide_video_mappings ALTER COLUMN validation_state DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE slide_video_mappings "
        "ALTER COLUMN validation_state "
        "TYPE mapping_validation_state_enum "
        "USING validation_state::mapping_validation_state_enum"
    )
    op.execute(
        "ALTER TABLE slide_video_mappings "
        "ALTER COLUMN validation_state "
        "SET DEFAULT 'pending_validation'::mapping_validation_state_enum"
    )

    op.create_index("ix_svm_node", "slide_video_mappings", ["node_id"])
    op.create_index(
        "ix_svm_presentation",
        "slide_video_mappings",
        ["presentation_entry_id"],
    )
    op.create_index("ix_svm_video", "slide_video_mappings", ["video_entry_id"])
    op.create_index(
        "ix_svm_validation",
        "slide_video_mappings",
        ["validation_state"],
        postgresql_where=sa.text("validation_state != 'validated'"),
    )


def downgrade() -> None:
    op.drop_table("slide_video_mappings")

    # Drop enum type
    sa.Enum(name="mapping_validation_state_enum").drop(op.get_bind(), checkfirst=True)

    # Recreate old table
    op.create_table(
        "slide_video_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("slide_number", sa.Integer(), nullable=False),
        sa.Column("video_timecode", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
