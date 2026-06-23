"""sprint-mentor T3: Mentor review signal columns.

Revision ID: mentor_t3_review_signals
Revises: mentor_t2_authored_doc_anchor
Create Date: 2026-06-23 00:00:00.000000

Lays the schema for every signal the new Mentor reads and writes, before the
review graph is built (T6). Adds four typed columns:

- ``homework_submissions.score`` — final (post-D10 denoising) review grade,
  lifted out of the ``review_result`` JSONB into a typed column (D1, KD15),
  range-guarded by ``ck_homework_score_range``.
- ``homework_submissions.student_note`` — D7-local student comment.
- ``students.mentor_preferences`` — D7-global standing review preferences.
- ``authored_documents.author_mentor_notes`` — D6 task-level author notes.

The layered ``review_result`` shape (per-layer judgments, aggregate score,
history reconciliation) is NOT pinned here — its exact form lands with the
review graph in T6; only the webhook-facing ``verdict`` key is consumed now
(build_reviewed_payload). No column change for ``review_result`` (already JSONB).

Forward-only: the three target tables are empty (homework not yet live on
prod), so the nullable adds need no backfill. The downgrade drops the columns
and the check constraint for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mentor_t3_review_signals"
down_revision = "mentor_t2_authored_doc_anchor"
branch_labels = None
depends_on = None

_HOMEWORK = "homework_submissions"
_STUDENTS = "students"
_AUTHORED = "authored_documents"
_SCORE_CHECK = "ck_homework_score_range"


def upgrade() -> None:
    """Add the four Mentor signal columns + the score range check."""
    op.add_column(
        _HOMEWORK,
        sa.Column(
            "score",
            sa.Integer(),
            nullable=True,
            comment=(
                "Final (post-D10 denoising) review score 0-100 — the headline "
                "grade delivered to the caller. D1 lifts it out of the "
                "review_result JSONB into a typed column (KD15); review_result "
                "keeps the per-layer and aggregate scores. Range enforced by "
                "ck_homework_score_range."
            ),
        ),
    )
    op.create_check_constraint(
        _SCORE_CHECK,
        _HOMEWORK,
        "score IS NULL OR (score BETWEEN 0 AND 100)",
    )
    op.add_column(
        _HOMEWORK,
        sa.Column(
            "student_note",
            sa.Text(),
            nullable=True,
            comment=(
                "D7-local: student's comment/question attached to this "
                "submission (free text)."
            ),
        ),
    )
    op.add_column(
        _STUDENTS,
        sa.Column(
            "mentor_preferences",
            sa.Text(),
            nullable=True,
            comment=(
                "D7-global: student's standing preferences on review "
                "style/limits (free text)."
            ),
        ),
    )
    op.add_column(
        _AUTHORED,
        sa.Column(
            "author_mentor_notes",
            sa.Text(),
            nullable=True,
            comment=(
                "D6: author notes that focus Mentor assessment at the task "
                "level (free text); shifts emphasis on the node/course "
                "criteria layers during review."
            ),
        ),
    )


def downgrade() -> None:
    """Reverse: drop the four columns and the score check (round-trip only)."""
    op.drop_column(_AUTHORED, "author_mentor_notes")
    op.drop_column(_STUDENTS, "mentor_preferences")
    op.drop_constraint(_SCORE_CHECK, _HOMEWORK, type_="check")
    op.drop_column(_HOMEWORK, "student_note")
    op.drop_column(_HOMEWORK, "score")
