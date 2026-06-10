"""Phase 3.1 commit 1: NodeSummary{Raw,Final,FinalPreviousSnapshot} tables.

Revision ID: phase31_node_summary_tables
Revises: phase24_root_lang_required
Create Date: 2026-06-10 00:00:00.000000

Creates the methodist-layer data tables per vision §3 KD9/KD10/KD11.
Three tables — Raw (system-generated), Final (editable, downstream
source of truth), FinalPreviousSnapshot (single-version revert
target before automatic Raw overwrite).

Each Final/Raw is 1:1 with CourseNode (UNIQUE on course_node_id);
PreviousSnapshot is 1:1 with Final (UNIQUE on node_summary_final_id).
``content_hash`` columns are created nullable here — server_default
+ NOT NULL lands in commit 4 (task 3.1) together with the KD9
NULL-at-INSERT regression fix on ``course_nodes``. The same applies
to enclosing-context columns and meta counts which carry their
runtime defaults via SQLAlchemy ``server_default`` at the ORM layer
(JSONB ``'[]'``, integers ``0``, booleans ``false``).

Soft-delete (``deleted_at`` + partial-active index) inherited from
``SoftDeleteMixin`` per KD3; cascade wiring lands in commit 3.

Self-contained migration — no app-code import per Phase 2.4.13
ratified corrective.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "phase31_node_summary_tables"
down_revision: str | Sequence[str] | None = "phase24_root_lang_required"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared JSONB type spelt out once for readability.
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Create the three NodeSummary tables + their indexes."""
    op.create_table(
        "node_summaries_raw",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            sa.ForeignKey("course_nodes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Identity
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # Learning outcomes
        sa.Column(
            "learning_objectives",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "knowledge",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "skills",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Assessment
        sa.Column(
            "success_criteria",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("assessment_approach", sa.Text(), nullable=True),
        # Methodology
        sa.Column("teaching_approach", sa.Text(), nullable=True),
        sa.Column(
            "key_activities",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "common_mistakes",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Concepts
        sa.Column(
            "main_concepts",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "secondary_concepts",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Cross-level context
        sa.Column("compressed_summary", sa.Text(), nullable=True),
        sa.Column("enclosing_context", sa.Text(), nullable=True),
        # Raw-only
        sa.Column(
            "methodist_observations",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Size metrics
        sa.Column(
            "own_documents_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "own_chars_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cumulative_documents_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cumulative_chars_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Hash axes — server_default + NOT NULL added in commit 4
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("enclosing_context_source_hash", sa.String(length=64), nullable=True),
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
        # Soft-delete
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        comment=(
            "System-generated methodist summary (vision §3 KD10) — "
            "1:1 with CourseNode; rule «є вузол, є Raw» (v0.20.x)"
        ),
    )
    op.create_index(
        "ix_node_summaries_raw_active",
        "node_summaries_raw",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_node_summaries_raw_course_node_id",
        "node_summaries_raw",
        ["course_node_id"],
    )

    op.create_table(
        "node_summaries_final",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            sa.ForeignKey("course_nodes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Editable
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "learning_objectives",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "knowledge",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "skills",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "success_criteria",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("assessment_approach", sa.Text(), nullable=True),
        sa.Column("teaching_approach", sa.Text(), nullable=True),
        sa.Column(
            "key_activities",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "common_mistakes",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Read-only copies from Raw
        sa.Column(
            "main_concepts",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "secondary_concepts",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("enclosing_context", sa.Text(), nullable=True),
        # Empty-leaf author flow
        sa.Column(
            "is_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("manual_description", sa.Text(), nullable=True),
        # Size metrics
        sa.Column(
            "own_documents_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "own_chars_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cumulative_documents_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cumulative_chars_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Hash axis — server_default + NOT NULL added in commit 4
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        # Approval pair
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enclosing_context_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
        # Soft-delete
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        comment=(
            "Editable canonical methodist summary (vision §3 KD11) — "
            "1:1 with CourseNode; downstream source of truth"
        ),
    )
    op.create_index(
        "ix_node_summaries_final_active",
        "node_summaries_final",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_node_summaries_final_course_node_id",
        "node_summaries_final",
        ["course_node_id"],
    )

    op.create_table(
        "node_summaries_final_previous_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "node_summary_final_id",
            sa.Uuid(),
            sa.ForeignKey("node_summaries_final.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "snapshot",
            _JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "replaced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        comment=(
            "Single prior version of NodeSummaryFinal (vision §3 KD11) — "
            "1:1; replaced on overwrite; hard-deleted on approve"
        ),
    )
    op.create_index(
        "ix_node_summaries_final_previous_snapshots_active",
        "node_summaries_final_previous_snapshots",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_node_summaries_final_previous_snapshots_final_id",
        "node_summaries_final_previous_snapshots",
        ["node_summary_final_id"],
    )


def downgrade() -> None:
    """Drop the three NodeSummary tables (PreviousSnapshot → Final → Raw)."""
    op.drop_index(
        "ix_node_summaries_final_previous_snapshots_final_id",
        table_name="node_summaries_final_previous_snapshots",
    )
    op.drop_index(
        "ix_node_summaries_final_previous_snapshots_active",
        table_name="node_summaries_final_previous_snapshots",
    )
    op.drop_table("node_summaries_final_previous_snapshots")

    op.drop_index(
        "ix_node_summaries_final_course_node_id",
        table_name="node_summaries_final",
    )
    op.drop_index(
        "ix_node_summaries_final_active",
        table_name="node_summaries_final",
    )
    op.drop_table("node_summaries_final")

    op.drop_index(
        "ix_node_summaries_raw_course_node_id",
        table_name="node_summaries_raw",
    )
    op.drop_index(
        "ix_node_summaries_raw_active",
        table_name="node_summaries_raw",
    )
    op.drop_table("node_summaries_raw")
