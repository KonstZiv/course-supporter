"""Phase 3.2.6 Finding 2: DocumentSummary active-only uniqueness.

Revision ID: phase326_summary_active_unique
Revises: phase322_raw_source_hash
Create Date: 2026-06-16 00:00:00.000000

Replaces the full column-level unique constraint on
``document_summaries.authored_document_id`` with a PARTIAL unique index
``WHERE deleted_at IS NULL``. ``DocumentSummary`` is a soft-delete table;
the old full unique meant reprocess (soft-delete the prior summary + insert
a fresh one) collided with the soft-deleted predecessor still occupying the
slot. The active-only invariant — at most one summary per AuthoredDocument
among non-soft-deleted rows — lets soft-deleted history coexist with the
single live row while preserving the KD-theta.2 1:1 contract on the active
slice.

Backward-safe: current data holds exactly one active summary per document
(every existing row satisfies the partial unique). Downgrade assumes no
soft-deleted duplicates exist (true for any DB that has not yet run a
3.2.6 reprocess); recreating the full constraint would otherwise fail.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "phase326_summary_active_unique"
down_revision = "phase322_raw_source_hash"
branch_labels = None
depends_on = None

_TABLE = "document_summaries"
_FULL_CONSTRAINT = "uq_document_summaries_authored_document_id"
_PARTIAL_INDEX = "uq_document_summaries_authored_document_id_active"


def upgrade() -> None:
    """Full unique constraint → partial-active unique index."""
    op.drop_constraint(_FULL_CONSTRAINT, _TABLE, type_="unique")
    op.create_index(
        _PARTIAL_INDEX,
        _TABLE,
        ["authored_document_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Restore the full (non-partial) unique constraint.

    Assumes no soft-deleted duplicate ``authored_document_id`` rows exist
    (pre-3.2.6 invariant); otherwise the full constraint cannot be created.
    """
    op.drop_index(_PARTIAL_INDEX, table_name=_TABLE)
    op.create_unique_constraint(
        _FULL_CONSTRAINT,
        _TABLE,
        ["authored_document_id"],
    )
