"""sprint-mentor T1: drop HomeworkSubmission.match_result.

Revision ID: mentor_t1_drop_match_result
Revises: phase33c_ib_job_duration
Create Date: 2026-06-23 00:00:00.000000

Task matching was removed in the green-field Mentor rewrite (vision D12,
KD15): a submission is anchored to its task via ``authored_document_id``,
so the legacy ``match_result`` JSONB (matched_node_id / task_type /
confidence) has no writer and no future use. Its writer (the HW-005
matching block) died in C9.3, so the column is always NULL post-C9.3.

Forward-only: NO data to preserve (always NULL). The downgrade re-adds
the nullable column. Sibling orphan ``matched_task_id`` is intentionally
kept (DD-2.1-AG, Phase 4 reroute) and is NOT touched here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "mentor_t1_drop_match_result"
down_revision = "phase33c_ib_job_duration"
branch_labels = None
depends_on = None

_TABLE = "homework_submissions"
_COLUMN = "match_result"


def upgrade() -> None:
    """Drop the orphan match_result column (always NULL post-C9.3)."""
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    """Re-add the nullable match_result column."""
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Task matching result (matched_node_id, task_type, confidence)",
        ),
    )
