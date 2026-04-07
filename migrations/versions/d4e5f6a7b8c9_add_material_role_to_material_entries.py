"""add_material_role_to_material_entries

Revision ID: d4e5f6a7b8c9
Revises: 523928fecdd4
Create Date: 2026-04-07 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "523928fecdd4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add material_role enum and column to material_entries."""
    op.execute(
        "CREATE TYPE material_role_enum AS ENUM ('educational', 'methodological')"
    )
    op.add_column(
        "material_entries",
        sa.Column(
            "material_role",
            sa.Enum(
                "educational",
                "methodological",
                name="material_role_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="educational",
            comment="Role: educational (delivers content) or methodological (declares intent)",
        ),
    )


def downgrade() -> None:
    """Remove material_role column and enum."""
    op.drop_column("material_entries", "material_role")
    op.execute("DROP TYPE IF EXISTS material_role_enum")
