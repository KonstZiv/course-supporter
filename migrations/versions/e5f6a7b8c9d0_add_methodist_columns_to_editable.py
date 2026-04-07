"""add_methodist_columns_to_editable

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-07 12:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Methodist output columns to structure_nodes_editable."""
    op.add_column(
        "structure_nodes_editable",
        sa.Column(
            "methodological_content",
            JSONB(),
            nullable=True,
            comment="MethodistNodeOutput JSON (structured Layer 3)",
        ),
    )
    op.add_column(
        "structure_nodes_editable",
        sa.Column(
            "methodological_markdown",
            sa.Text(),
            nullable=True,
            comment="Rendered Markdown for author review",
        ),
    )
    op.add_column(
        "structure_nodes_editable",
        sa.Column(
            "methodist_call_id",
            sa.Uuid(),
            sa.ForeignKey("external_service_calls.id", ondelete="SET NULL"),
            nullable=True,
            comment="FK to ExternalServiceCall that produced Methodist output",
        ),
    )


def downgrade() -> None:
    """Remove Methodist output columns from structure_nodes_editable."""
    op.drop_column("structure_nodes_editable", "methodist_call_id")
    op.drop_column("structure_nodes_editable", "methodological_markdown")
    op.drop_column("structure_nodes_editable", "methodological_content")
