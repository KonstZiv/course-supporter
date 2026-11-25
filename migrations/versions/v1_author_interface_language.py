"""V1: the author's panel interface language.

Revision ID: v1_author_interface_language
Revises: o1_open_enrollment
Create Date: 2026-11-24

The author panel becomes bilingual, and the operator decided that the page
language an author picks is kept by the server on the author's record (the
tenant), so the panel opens in it right after login on any device. The
student's counterpart is M-3 (``students.interface_language``).

Adds ``tenants.interface_language`` — nullable ``String(10)``, no default:
the ISO 639-3 code of the author panel pages' language, normalised on entry;
NULL means the author has not chosen yet. Nothing on the server reads the new
column beyond serving it back to the panel: letters, the Mentor and billing
do not, and the students' review languages are untouched.

Forward-only: the column is a nullable add and nothing is backfilled.
Downgrade drops the column for round-trip integrity.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v1_author_interface_language"
down_revision = "o1_open_enrollment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``tenants.interface_language``."""
    op.add_column(
        "tenants",
        sa.Column(
            "interface_language",
            sa.String(length=10),
            nullable=True,
            comment=(
                "мова сторінок панелі автора, обрана автором; ISO 639-3, "
                "нормалізована на вході; NULL — ще не обирав"
            ),
        ),
    )


def downgrade() -> None:
    """Drop ``tenants.interface_language``."""
    op.drop_column("tenants", "interface_language")
