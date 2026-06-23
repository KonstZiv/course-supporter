"""sprint-mentor T2: anchor HomeworkSubmission to its task via authored_document_id.

Revision ID: mentor_t2_authored_doc_anchor
Revises: mentor_t1_drop_match_result
Create Date: 2026-06-23 00:00:00.000000

Vision KD15 declares ``authored_document_id`` the single anchor FK from a
HomeworkSubmission to the AuthoredDocument (task) it answers — tenant,
course, and ``task_type`` derive through it. This migration introduces that
FK (NOT NULL, ondelete RESTRICT) and retires the two legacy task-pointers it
subsumes: ``task_hint_id`` (a non-FK caller hint) and the ``matched_task_id``
orphan (its writer died in C9.3; the DD-2.1-AG reroute resolves here).

Forward-only: ``homework_submissions`` is empty (homework not yet live on
prod), so the NOT NULL add needs no backfill. The downgrade reverses the
schema (re-adds both legacy columns, nullable) for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mentor_t2_authored_doc_anchor"
down_revision = "mentor_t1_drop_match_result"
branch_labels = None
depends_on = None

_TABLE = "homework_submissions"
_FK_NAME = "homework_submissions_authored_document_id_fkey"
_FK_INDEX = "ix_homework_submissions_authored_document_id"
_MATCHED_INDEX = "ix_homework_submissions_matched_task_id"


def upgrade() -> None:
    """Add the authored_document_id anchor; drop task_hint_id + matched_task_id."""
    op.add_column(
        _TABLE,
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task). Single submission↔task "
                "anchor (KD15): tenant, course, and task_type derive through "
                "it. RESTRICT — AuthoredDocument soft-delete does not cascade "
                "to submissions (D3: submissions are student history, not task "
                "derivatives)."
            ),
        ),
    )
    op.create_index(_FK_INDEX, _TABLE, ["authored_document_id"])
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "authored_documents",
        ["authored_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Retire the legacy task-pointers subsumed by the anchor.
    op.drop_index(_MATCHED_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "matched_task_id")
    op.drop_column(_TABLE, "task_hint_id")


def downgrade() -> None:
    """Reverse: re-add legacy columns; drop the anchor FK (round-trip only)."""
    op.add_column(
        _TABLE,
        sa.Column(
            "task_hint_id",
            sa.Uuid(),
            nullable=True,
            comment="Optional task ID hint from the caller (not FK, validated in app)",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "matched_task_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "Orphan column post-C9.3 — FK to structure_nodes_editable "
                "dropped together with the table in C9.4. Writer is the "
                "HW-005 task-matching block (deleted in C9.3); reader is "
                "the webhook payload (always None post-C9.3). Column "
                "retained per DD-2.1-AG for Phase 4 reroute on "
                "NodeSummaryFinal; drop_column deferred to natural "
                "cleanup cycle."
            ),
        ),
    )
    op.create_index(_MATCHED_INDEX, _TABLE, ["matched_task_id"])

    op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")
    op.drop_index(_FK_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "authored_document_id")
