"""Expand MaterialNode: add tenant_id, learning_goal, expected_knowledge/skills.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-03-05 14:00:00.000000+00:00
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e5f6a7"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    # 1. Add columns (nullable first for tenant_id)
    op.add_column(
        "material_nodes",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "material_nodes",
        sa.Column("learning_goal", sa.Text(), nullable=True),
    )
    op.add_column(
        "material_nodes",
        sa.Column("expected_knowledge", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "material_nodes",
        sa.Column("expected_skills", postgresql.JSONB(), nullable=True),
    )

    # 2. Data migration: copy tenant_id from Course for all nodes
    op.execute(
        """
        UPDATE material_nodes mn
        SET tenant_id = c.tenant_id
        FROM courses c
        WHERE mn.course_id = c.id
        """
    )

    # 3. Make tenant_id NOT NULL
    op.alter_column("material_nodes", "tenant_id", nullable=False)

    # 4. Add FK constraint and index
    op.create_foreign_key(
        "fk_material_nodes_tenant",
        "material_nodes",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_material_nodes_tenant_id",
        "material_nodes",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_nodes_tenant_id", table_name="material_nodes")
    op.drop_constraint("fk_material_nodes_tenant", "material_nodes", type_="foreignkey")
    op.drop_column("material_nodes", "expected_skills")
    op.drop_column("material_nodes", "expected_knowledge")
    op.drop_column("material_nodes", "learning_goal")
    op.drop_column("material_nodes", "tenant_id")
