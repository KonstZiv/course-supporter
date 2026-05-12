"""Phase 2.1 C6: add authored_documents.safety_result JSONB column

Revision ID: phase21_authored_safety_result
Revises: phase1_rename_and_kd3
Create Date: 2026-05-12 00:00:00.000000

Adds a nullable ``safety_result`` JSONB column on ``authored_documents``
to persist the Stage 2 LLM safety verdict (KD-2.1-P defense-in-depth).
Both pass and reject outcomes are persisted; column stays NULL until
Stage 2 runs (i.e., legacy rows pre-Phase-2.1 C6 keep NULL).

JSON shape matches :class:`course_supporter.security.schemas.SafetyResult`
(``source="stage2"``; ``is_safe``, ``violations``, ``confidence``,
``reasoning``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "phase21_authored_safety_result"
down_revision: str | Sequence[str] | None = "phase1_rename_and_kd3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable safety_result JSONB column to authored_documents."""
    op.add_column(
        "authored_documents",
        sa.Column(
            "safety_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Stage 2 LLM safety verdict persisted regardless of "
                "is_safe outcome. JSON shape per security.schemas."
                "SafetyResult (source='stage2'; is_safe, violations, "
                "confidence, reasoning). NULL when Stage 2 hasn't run "
                "yet (legacy rows pre-Phase-2.1 C6)."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the safety_result column."""
    op.drop_column("authored_documents", "safety_result")
