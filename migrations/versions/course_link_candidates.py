"""mentor-rebuild task 10, commit K4: the course links found for a submission.

Revision ID: course_link_candidates
Revises: esc_billing_grade_cost
Create Date: 2026-10-30

Creates one table, a leaf of ``homework_submissions``:

``submission_course_link_candidates`` — what the search for course links found
for one submission's failed criteria: places in the course where a criterion's
topic is explained. One row per submission, kept for the stages after the
search, which choose among the candidates and explain them: a path's checkpoint
keeps no stage output. A repeat run of the search replaces the row, which the
unique index on ``submission_id`` holds. Shaped after
``submission_explanations`` (``submission_verdict_tables``).

It carries ``tenant_id`` (every read is scoped by it, ``impl-rules#9``) and a
foreign key to the submission with ``ON DELETE CASCADE``, so a hard delete of
the submission takes the row; no ``deleted_at`` — as on the explanations, the
readers filter on the live submission.

Column and table comments are copied from the ORM; a test compares the live
ones with it.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). The ORM mirror is
``storage.orm.SubmissionCourseLinkCandidates``. A new table rewrites no row.
The downgrade drops it; lossless ONLY on a database where nothing has written
it yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "course_link_candidates"
down_revision: str | Sequence[str] | None = "esc_billing_grade_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CANDIDATES = "submission_course_link_candidates"


def upgrade() -> None:
    """Create the table with its tenant index and its unique index."""
    op.create_table(
        _CANDIDATES,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → Tenant. Every read and write of candidates is scoped by it.",
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("homework_submissions.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → HomeworkSubmission the candidates are found for.",
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The candidates, as the search step's own model dumps them.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the candidates were first written.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment=(
                "When they were last replaced. On the ON CONFLICT DO UPDATE "
                "path this column is set EXPLICITLY: SQLAlchemy does not apply a "
                "Python-side onupdate to an upsert (dialects/postgresql/dml.py)."
            ),
        ),
        comment=(
            "Course links found for one submission's failed criteria "
            "(mentor-rebuild task 10), kept for the stages after the search. "
            "One row per submission; a repeat run replaces it."
        ),
    )
    op.create_index(
        "ix_submission_course_link_candidates_tenant_id", _CANDIDATES, ["tenant_id"]
    )
    op.create_index(
        "uq_submission_course_link_candidates_submission_id",
        _CANDIDATES,
        ["submission_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the table (its indexes go with it)."""
    op.drop_table(_CANDIDATES)
