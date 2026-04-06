"""add_outline_content_to_material_entries

Revision ID: 523928fecdd4
Revises: c3d4e5f6a7b8
Create Date: 2026-04-06 14:02:23.233024

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "523928fecdd4"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add outline_content column to material_entries."""
    op.add_column(
        "material_entries",
        sa.Column(
            "outline_content",
            sa.Text(),
            nullable=True,
            comment="MaterialOutline JSON (lossless restructuring of processed_content)",
        ),
    )


def downgrade() -> None:
    """Remove outline_content column from material_entries."""
    op.drop_column("material_entries", "outline_content")
