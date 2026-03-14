"""add reconciliation_previews table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_previews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("materialnode_id", sa.Uuid(), nullable=False),
        sa.Column("combined_fingerprint", sa.String(128), nullable=False),
        sa.Column("node_fingerprint", sa.String(64), nullable=False),
        sa.Column("editable_tree_hash", sa.String(64), nullable=False),
        sa.Column(
            "issues",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["materialnode_id"],
            ["material_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            ondelete="SET NULL",
        ),
        comment=(
            "Cached reconciliation preview results keyed by "
            "combined fingerprint (node materials + editable tree)"
        ),
    )
    op.create_index(
        "ix_reconciliation_previews_materialnode_id",
        "reconciliation_previews",
        ["materialnode_id"],
    )
    op.create_index(
        "ix_reconciliation_previews_job_id",
        "reconciliation_previews",
        ["job_id"],
    )
    op.create_index(
        "uq_recon_preview_identity",
        "reconciliation_previews",
        ["materialnode_id", "combined_fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_recon_preview_identity",
        table_name="reconciliation_previews",
    )
    op.drop_index(
        "ix_reconciliation_previews_job_id",
        table_name="reconciliation_previews",
    )
    op.drop_index(
        "ix_reconciliation_previews_materialnode_id",
        table_name="reconciliation_previews",
    )
    op.drop_table("reconciliation_previews")
