"""M-3: the student's portal interface language.

Revision ID: m3_interface_language
Revises: task12a_remark_comments
Create Date: 2026-11-20

The student portal becomes bilingual, and the operator decided that the page
language a student picks is kept by the server on the student's record, so
the student sees it right after login on any device.

Adds ``students.interface_language`` — nullable ``String(10)``, no default:
the ISO 639-3 code of the portal pages' language, normalised on entry; NULL
means the student has not chosen yet. It is a field of its own: the review
language (``students.preferred_language``) is untouched, and nothing on the
server reads the new column beyond serving it back to the portal.

Forward-only: the column is a nullable add and nothing is backfilled.
Downgrade drops the column for round-trip integrity.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "m3_interface_language"
down_revision = "task12a_remark_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``students.interface_language``."""
    op.add_column(
        "students",
        sa.Column(
            "interface_language",
            sa.String(length=10),
            nullable=True,
            comment=(
                "мова сторінок порталу, обрана студентом; ISO 639-3, "
                "нормалізована на вході; NULL — ще не обирав"
            ),
        ),
    )


def downgrade() -> None:
    """Drop ``students.interface_language``."""
    op.drop_column("students", "interface_language")
