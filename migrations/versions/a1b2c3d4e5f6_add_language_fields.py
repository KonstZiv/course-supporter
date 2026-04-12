"""add_language_fields_to_material_entries_and_nodes

Adds:
- material_nodes.default_language (ISO 639-1, usually set on root/course)
- material_entries.language (ISO 639-1, overrides course default; also
  populated from STT auto-detection on first successful transcription)

Revision ID: a1b2c3d4e5f6
Revises: 606fdf83013b
Create Date: 2026-04-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "606fdf83013b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add language columns to material_nodes and material_entries."""
    op.add_column(
        "material_nodes",
        sa.Column(
            "default_language",
            sa.String(length=10),
            nullable=True,
            comment=(
                "Default ISO 639-1 language for materials under this subtree. "
                "Usually set on the root (course) node; inherited by children "
                "and their materials unless overridden on the material itself."
            ),
        ),
    )
    op.add_column(
        "material_entries",
        sa.Column(
            "language",
            sa.String(length=10),
            nullable=True,
            comment=(
                "ISO 639-1 language of the material. NULL = inherit from course "
                "(root MaterialNode.default_language) or auto-detect at STT "
                "time. Auto-detected language is cached back to this column on "
                "first success."
            ),
        ),
    )


def downgrade() -> None:
    """Drop language columns."""
    op.drop_column("material_entries", "language")
    op.drop_column("material_nodes", "default_language")
