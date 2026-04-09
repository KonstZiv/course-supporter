"""add file_hash to homework_submissions

Revision ID: 34442e990ee7
Revises: ec8afe68a0e6
Create Date: 2026-04-09 10:09:23.624860

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "34442e990ee7"
down_revision: Union[str, Sequence[str], None] = "ec8afe68a0e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add file_hash column and index to homework_submissions."""
    op.add_column(
        "homework_submissions",
        sa.Column(
            "file_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 hex digest for deduplication",
        ),
    )
    op.create_index(
        op.f("ix_homework_submissions_file_hash"),
        "homework_submissions",
        ["file_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Remove file_hash column and index from homework_submissions."""
    op.drop_index(
        op.f("ix_homework_submissions_file_hash"),
        table_name="homework_submissions",
    )
    op.drop_column("homework_submissions", "file_hash")
