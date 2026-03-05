"""remove content_fingerprint from material_entries

Revision ID: 63c6565a0e1b
Revises: a8f1e2c3d4b5
Create Date: 2026-03-05 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "63c6565a0e1b"
down_revision: Union[str, None] = "a8f1e2c3d4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("material_entries", "content_fingerprint")


def downgrade() -> None:
    op.add_column(
        "material_entries",
        sa.Column("content_fingerprint", sa.String(64), nullable=True),
    )
