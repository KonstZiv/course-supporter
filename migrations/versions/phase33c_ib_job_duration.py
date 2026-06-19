"""Phase 3.3c-I-B: Job.duration_sec for the admission gate.

Revision ID: phase33c_ib_job_duration
Revises: phase33b_concept_search_gin
Create Date: 2026-06-19 00:00:00.000000

Adds a nullable ``duration_sec`` column to ``jobs`` so the admission gate
(DD-3.3c-I-B) can sum pending+active video-hours already in the queue and
refuse intake once the queue is at capacity. Filled at intake by an
ffprobe (upload / presigned) or a yt-dlp metadata probe (URL) BEFORE
enqueue; NULL for non-video ingests and for any pre-existing row.

Forward-only: NO backfill. Existing jobs keep NULL — they contribute 0 to
the aggregate (``SUM`` ignores NULL), which is conservative (slightly
under-counts the in-flight queue at rollout, self-correcting as old jobs
drain and new ones carry the probed value). The downgrade drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "phase33c_ib_job_duration"
down_revision = "phase33b_concept_search_gin"
branch_labels = None
depends_on = None

_TABLE = "jobs"
_COLUMN = "duration_sec"


def upgrade() -> None:
    """Add the nullable duration_sec column (no backfill)."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the duration_sec column."""
    op.drop_column(_TABLE, _COLUMN)
