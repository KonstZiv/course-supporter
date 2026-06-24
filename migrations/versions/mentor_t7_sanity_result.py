"""sprint-mentor T7: sanity gate result column.

Revision ID: mentor_t7_sanity_result
Revises: mentor_t4_criteria_cache
Create Date: 2026-06-24 00:00:00.000000

Adds ``homework_submissions.sanity_result`` (JSONB, nullable) — the lightweight
sanity gate verdict T7 introduces between the safety and review stages
(KD15 §1300): ``{verdict, confidence, reason}`` answering "does the submission
look like an attempt at the declared task?". A high-confidence mismatch
terminates the pipeline (status ``mismatch``) before the expensive review
graph; T3 deliberately deferred this column to T7, its single consumer.

Forward-only: ``homework_submissions`` is empty (homework not yet live on
prod), so the nullable add needs no backfill. Downgrade drops the column for
round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "mentor_t7_sanity_result"
down_revision = "mentor_t4_criteria_cache"
branch_labels = None
depends_on = None

_HOMEWORK = "homework_submissions"


def upgrade() -> None:
    """Add the nullable ``sanity_result`` JSONB column."""
    op.add_column(
        _HOMEWORK,
        sa.Column(
            "sanity_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Sanity gate result (sprint-mentor T7, KD15): "
                "{verdict, confidence, reason} — does the submission look like "
                "an attempt at the declared task? A high-confidence mismatch "
                "terminates the pipeline before the (expensive) review graph; "
                "match or a low-confidence mismatch passes through to review "
                "unchanged."
            ),
        ),
    )


def downgrade() -> None:
    """Reverse: drop the column (round-trip only)."""
    op.drop_column(_HOMEWORK, "sanity_result")
