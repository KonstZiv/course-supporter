"""mentor-rebuild task 05: what the student says about a review.

Revision ID: student_feedback_touch
Revises: esc_stage_money_ceiling
Create Date: 2026-09-18

Creates ``student_feedback`` — one row per (target, student), holding the one
thing a student can say in the first echelon: whether the review helped. The
shape is the ratified one (``05-feedback/TASK.md``, the section of decisions
taken from the probe report):

* The target is a PAIR — ``target_kind`` + ``target_id`` — with NO foreign key.
  The column addresses a different table per kind (today a submission's review;
  later a remark, a library link), and a polymorphic FK does not exist. What a
  FK would buy is bought by the single writer, which reads the submission and
  its review before writing.
* ``uq_student_feedback_target`` on (target_kind, target_id, student_id) is what
  makes a repeat touch a REPLACEMENT rather than a second row — the rule is in
  the database because two requests can race and only the database decides such
  a race the same way every time.
* NO ``deleted_at`` and no soft-delete trigger: a cascade is resolved by the
  foreign key this table deliberately does not have, and the read paths filter
  on the live submission and the live student instead.
* The three vocabularies are ``CHECK`` constraints, and their values are spelled
  out LITERALLY here on purpose: the ORM builds the same lists from
  ``FeedbackTargetKind`` / ``FeedbackKind`` / ``FeedbackValue``, and a migration
  must not change meaning when the code later does. Widening one is a new
  migration that re-issues the CHECK — Postgres has no "widen a CHECK".

Hand-written (SPRINT rule 6 of §2). ``alembic revision --autogenerate`` is NOT
used and its output is NOT included: on this database it reports ~200 operations
of pre-existing whole-schema comment and index drift (``DD-L4-A``), of which the
ones belonging to this task are the table below and nothing else.

Forward-only: a brand-new table, no back-fill — no row can exist yet. The
downgrade drops it for round-trip integrity.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "student_feedback_touch"
down_revision: str | Sequence[str] | None = "esc_stage_money_ceiling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "student_feedback"

# Spelled out, not derived from the enums: see the module docstring.
_TARGET_KINDS = "'review'"
_KINDS = "'touch'"
_VALUES = "'helped', 'not_helped'"


def upgrade() -> None:
    """Create ``student_feedback`` with its three CHECKs and one unique index."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → Tenant. Every read and write of feedback is scoped by it.",
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → Student (the author of the feedback).",
        ),
        sa.Column(
            "target_kind",
            sa.String(length=32),
            nullable=False,
            comment=(
                "What the feedback points at (FeedbackTargetKind). Today: "
                "'review'. A second-echelon target is a new member, not a new "
                "table."
            ),
        ),
        sa.Column(
            "target_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "Identifier of the target — for 'review', the "
                "HomeworkSubmission whose review it is. NO foreign key: the "
                "column addresses different tables per kind; the writing core "
                "validates it."
            ),
        ),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment="The kind of feedback (FeedbackKind). Today: 'touch'.",
        ),
        sa.Column(
            "value",
            sa.String(length=32),
            nullable=False,
            comment="The answer (FeedbackValue): 'helped' or 'not_helped'.",
        ),
        sa.Column(
            "text",
            sa.Text(),
            nullable=True,
            comment=(
                "Optional free text. Always NULL in task 05 — a touch has no "
                "text; the column is here because a reply is the same record."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the student first answered about this target.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment=(
                "When the answer last changed. On the ON CONFLICT DO UPDATE "
                "path this column is set EXPLICITLY: SQLAlchemy does not apply "
                "a Python-side onupdate to an upsert "
                "(dialects/postgresql/dml.py)."
            ),
        ),
        sa.CheckConstraint(
            f"target_kind IN ({_TARGET_KINDS})",
            name="ck_student_feedback_target_kind",
        ),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_student_feedback_kind"),
        sa.CheckConstraint(f"value IN ({_VALUES})", name="ck_student_feedback_value"),
        comment=(
            "One student's feedback on one target (mentor-rebuild task 05). "
            "One row per (target, student): a repeat touch replaces the "
            "previous one. Counters are derived on read, never stored."
        ),
    )
    op.create_index("ix_student_feedback_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_student_feedback_student_id", _TABLE, ["student_id"])
    op.create_index(
        "uq_student_feedback_target",
        _TABLE,
        ["target_kind", "target_id", "student_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the table (its indexes and constraints go with it)."""
    op.drop_table(_TABLE)
