"""material_nodes.order: drop NOT NULL, allow null for "no preferred order"

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-15 21:00:00.000000

NULL semantics: "no preferred sort position". Sort is
``ORDER BY order ASC NULLS LAST, created_at ASC`` — NULL siblings appear
last, sorted by creation time. Existing rows keep their integer values
(default 0 from prior schema), no data migration required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop NOT NULL from material_nodes.order."""
    op.alter_column(
        "material_nodes",
        "order",
        existing_type=sa.Integer(),
        nullable=True,
        server_default=None,
        existing_server_default=None,
        comment=(
            "Sibling sort key within parent. NULL = no preferred order, "
            "sorted last by created_at. Sort: ORDER BY order ASC NULLS LAST, "
            "created_at ASC."
        ),
    )


def downgrade() -> None:
    """Restore NOT NULL on material_nodes.order.

    Backfills any NULL rows to 0 before applying the constraint to avoid
    rollback failure on rows that were inserted as NULL while column was
    nullable.
    """
    op.execute('UPDATE material_nodes SET "order" = 0 WHERE "order" IS NULL')
    op.alter_column(
        "material_nodes",
        "order",
        existing_type=sa.Integer(),
        nullable=False,
        comment=None,
    )
