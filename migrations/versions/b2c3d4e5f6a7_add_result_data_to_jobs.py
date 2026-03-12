"""add result_data to jobs

Revision ID: b2c3d4e5f6a7
Revises: a1f2e3d4b5c6
Create Date: 2026-03-12 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1f2e3d4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add result_data JSONB column to jobs table."""
    op.add_column(
        "jobs",
        sa.Column(
            "result_data",
            JSONB(),
            nullable=True,
            comment="JSONB result payload (e.g. reconciliation preview issues)",
        ),
    )


def downgrade() -> None:
    """Remove result_data column from jobs table."""
    op.drop_column("jobs", "result_data")
