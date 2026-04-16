"""add material_macro_sections + material_segments tables

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-04-16 15:00:00.000000

PR #4a of Content Ingestion roadmap: schema only, no reads/writes from
pipeline code. Introduces the two-level Table-Of-Contents model that
will replace the monolithic ``MaterialEntry.outline_content`` JSON
blob in subsequent PRs.

- ``material_macro_sections`` — TOC-level sections within a MaterialEntry
  (Stage 5 pipeline output)
- ``material_segments`` — cleaned/sliced content inside a macro section
  (Stage 6 pipeline output)

Position semantics live in column comments; unit depends on the parent
MaterialEntry.source_type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create macro sections + segments tables and supporting indexes."""
    op.create_table(
        "material_macro_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "material_entry_id",
            sa.Uuid(),
            nullable=False,
            comment="FK to parent MaterialEntry",
        ),
        sa.Column(
            "order",
            sa.Integer(),
            nullable=False,
            comment="0-indexed position within the TOC of this material entry",
        ),
        sa.Column(
            "title",
            sa.String(length=500),
            nullable=False,
            comment="Self-contained section title",
        ),
        sa.Column(
            "start_pos",
            sa.Integer(),
            nullable=False,
            comment=(
                "Start position in the source-specific unit "
                "(ms for video/audio, 1-indexed slide for presentation, "
                "char offset for text/web)"
            ),
        ),
        sa.Column(
            "end_pos",
            sa.Integer(),
            nullable=False,
            comment=(
                "End position. Exclusive for video/audio/text/web, "
                "inclusive for presentation"
            ),
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ready",
                "failed",
                name="material_macro_section_status_enum",
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="Lifecycle status: pending → ready | failed",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "llm_call_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "FK to ExternalServiceCall that produced this section. "
                "NULL for deterministic (text/web) sections"
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["material_entry_id"],
            ["material_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["external_service_calls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("start_pos >= 0", name="ck_macro_start_pos_nonneg"),
        sa.CheckConstraint("end_pos > start_pos", name="ck_macro_end_pos_gt_start"),
        comment=(
            "Table-of-contents level sections within a MaterialEntry "
            "(Stage 5 output of the ingestion pipeline)"
        ),
    )
    op.create_index(
        op.f("ix_material_macro_sections_material_entry_id"),
        "material_macro_sections",
        ["material_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_material_macro_sections_entry_order",
        "material_macro_sections",
        ["material_entry_id", "order"],
        unique=False,
    )
    op.create_index(
        "ix_material_macro_sections_unready",
        "material_macro_sections",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status != 'ready'"),
    )

    op.create_table(
        "material_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "macro_section_id",
            sa.Uuid(),
            nullable=False,
            comment="FK to parent MaterialMacroSection",
        ),
        sa.Column(
            "order",
            sa.Integer(),
            nullable=False,
            comment="0-indexed position within the parent macro section",
        ),
        sa.Column(
            "start_pos",
            sa.Integer(),
            nullable=False,
            comment=(
                "Absolute start in the source unit (not relative to the macro section)"
            ),
        ),
        sa.Column(
            "end_pos",
            sa.Integer(),
            nullable=False,
            comment="Absolute end in the source unit",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Cleaned (LLM) or sliced (text/web) content of this segment",
        ),
        sa.Column(
            "llm_call_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "FK to ExternalServiceCall that produced this segment. "
                "NULL for deterministic (text/web) slices"
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["macro_section_id"],
            ["material_macro_sections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["external_service_calls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("start_pos >= 0", name="ck_segment_start_pos_nonneg"),
        sa.CheckConstraint("end_pos > start_pos", name="ck_segment_end_pos_gt_start"),
        comment=(
            "Cleaned / sliced content segments inside a "
            "MaterialMacroSection (Stage 6 output)"
        ),
    )
    op.create_index(
        op.f("ix_material_segments_macro_section_id"),
        "material_segments",
        ["macro_section_id"],
        unique=False,
    )
    op.create_index(
        "ix_material_segments_macro_order",
        "material_segments",
        ["macro_section_id", "order"],
        unique=False,
    )
    op.create_index(
        "ix_material_segments_macro_start_pos",
        "material_segments",
        ["macro_section_id", "start_pos"],
        unique=False,
    )


def downgrade() -> None:
    """Drop segments and macro sections tables plus the status enum."""
    op.drop_index(
        "ix_material_segments_macro_start_pos", table_name="material_segments"
    )
    op.drop_index("ix_material_segments_macro_order", table_name="material_segments")
    op.drop_index(
        op.f("ix_material_segments_macro_section_id"),
        table_name="material_segments",
    )
    op.drop_table("material_segments")

    op.drop_index(
        "ix_material_macro_sections_unready",
        table_name="material_macro_sections",
    )
    op.drop_index(
        "ix_material_macro_sections_entry_order",
        table_name="material_macro_sections",
    )
    op.drop_index(
        op.f("ix_material_macro_sections_material_entry_id"),
        table_name="material_macro_sections",
    )
    op.drop_table("material_macro_sections")

    op.execute("DROP TYPE IF EXISTS material_macro_section_status_enum")
