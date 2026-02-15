"""add_tenant_id_to_courses_and_llm_calls

Three-step migration:
1. Add tenant_id as NULLABLE
2. Create 'system' tenant and backfill existing rows
3. Set NOT NULL, add FK constraints and indexes

Revision ID: ec62b2f8d538
Revises: bb847e98ee7b
Create Date: 2026-02-15 15:46:07.634156

"""

from typing import Sequence, Union

import uuid_utils as uuid7_lib
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ec62b2f8d538"
down_revision: Union[str, Sequence[str], None] = "bb847e98ee7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenant_id to courses and llm_calls with backfill."""
    # 1. Add columns as NULLABLE
    op.add_column("courses", sa.Column("tenant_id", sa.Uuid(), nullable=True))
    op.add_column("llm_calls", sa.Column("tenant_id", sa.Uuid(), nullable=True))

    # 2. Create system tenant and backfill existing rows
    conn = op.get_bind()
    system_tenant_id = str(uuid7_lib.uuid7())
    conn.execute(
        sa.text("INSERT INTO tenants (id, name, is_active) VALUES (:id, :name, true)"),
        {"id": system_tenant_id, "name": "system"},
    )
    conn.execute(
        sa.text("UPDATE courses SET tenant_id = :tid WHERE tenant_id IS NULL"),
        {"tid": system_tenant_id},
    )
    conn.execute(
        sa.text("UPDATE llm_calls SET tenant_id = :tid WHERE tenant_id IS NULL"),
        {"tid": system_tenant_id},
    )

    # 3. Set NOT NULL + FK + index
    op.alter_column("courses", "tenant_id", nullable=False)
    op.alter_column("llm_calls", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_courses_tenant_id",
        "courses",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_llm_calls_tenant_id",
        "llm_calls",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_courses_tenant_id", "courses", ["tenant_id"])
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])


def downgrade() -> None:
    """Remove tenant_id from courses and llm_calls."""
    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
    op.drop_index("ix_courses_tenant_id", table_name="courses")
    op.drop_constraint("fk_llm_calls_tenant_id", "llm_calls", type_="foreignkey")
    op.drop_constraint("fk_courses_tenant_id", "courses", type_="foreignkey")
    op.drop_column("llm_calls", "tenant_id")
    op.drop_column("courses", "tenant_id")
    # Note: system tenant row is NOT removed (idempotent — harmless if kept)
