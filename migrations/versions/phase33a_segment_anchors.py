"""Phase 3.3a: DocumentSegment positional anchors.

Revision ID: phase33a_segment_anchors
Revises: phase326_summary_active_unique
Create Date: 2026-06-16 00:00:00.000000

Adds six nullable positional-anchor columns to ``document_segments`` so the
pipeline persists the source-type-specific position it already computes
(time for audio/video — KD-2.2-F; slide for presentation — KD-2.3-Q;
paragraph for text/web — Phase 3.3a net-new). These are raw machine anchors
for the Phase 3.3b concept-navigation surface (vision §4), NOT content
identity — they are deliberately excluded from the segment content_hash.

Each row fills exactly ONE pair per source_type; the rest stay NULL.

Forward-only: NO backfill. Existing segments keep NULL anchors — the
transient transcript timecodes / cumsum anchors are not recoverable from
stored rows, so populated values arrive only on reprocess. The downgrade
drops all six columns.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "phase33a_segment_anchors"
down_revision = "phase326_summary_active_unique"
branch_labels = None
depends_on = None

_TABLE = "document_segments"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("start_time_sec", sa.Float()),
    ("end_time_sec", sa.Float()),
    ("start_slide", sa.Integer()),
    ("end_slide", sa.Integer()),
    ("start_paragraph", sa.Integer()),
    ("end_paragraph", sa.Integer()),
)


def upgrade() -> None:
    """Add the six nullable positional-anchor columns (no backfill)."""
    for name, col_type in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    """Drop the six positional-anchor columns."""
    for name, _ in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
