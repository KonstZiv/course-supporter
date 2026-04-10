"""add response_language and preferred_language

Revision ID: 606fdf83013b
Revises: 34442e990ee7
Create Date: 2026-04-09 11:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "606fdf83013b"
down_revision: Union[str, Sequence[str], None] = "34442e990ee7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add language columns to students and homework_submissions."""
    op.add_column(
        "students",
        sa.Column(
            "preferred_language",
            sa.String(length=10),
            nullable=True,
            comment="ISO 639-1 language code from last review (e.g. uk, en)",
        ),
    )
    op.add_column(
        "homework_submissions",
        sa.Column(
            "response_language",
            sa.String(length=10),
            nullable=True,
            comment="ISO 639-1 language used for the review response",
        ),
    )


def downgrade() -> None:
    """Remove language columns."""
    op.drop_column("homework_submissions", "response_language")
    op.drop_column("students", "preferred_language")
