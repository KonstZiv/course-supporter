"""BIL-5: the mark of a service student (``students.is_test_student``).

Revision ID: biltest_student_flag
Revises: m3_interface_language
Create Date: 2026-11-20

The platform has service students of its own — control, rehearsal, a payment
provider reviewer's students. Their work is reviewed for real, but it skews the
calibration of the review cost estimate (``scripts/cost_estimate_report.py``):
our control archives are much larger than a typical student's work. Nothing in
the database told them apart; the mark is the one source the calibration
filters by (DD-BIL-CAL).

Adds ``students.is_test_student`` — ``Boolean NOT NULL DEFAULT false``. The
column comment is copied from the ORM (``storage.orm.Student``). Agreed with
the owner of the student model (19.11): a field on the student, not on the
billing account; the review path does not read it.

Forward-only: the column is added with its default and nothing is filled in —
every existing student reads ``false``; an administrator marks one with
``scripts/billing_admin.py test-student``. Downgrade drops the column.
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "biltest_student_flag"
down_revision = "m3_interface_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``students.is_test_student``."""
    op.add_column(
        "students",
        sa.Column(
            "is_test_student",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "A service student of the platform (a control, rehearsal or "
                "payment provider reviewer's student): their work is left out "
                "of the calibration of the review cost estimate. The review "
                "path does not read it."
            ),
        ),
    )


def downgrade() -> None:
    """Drop ``students.is_test_student``."""
    op.drop_column("students", "is_test_student")
