"""Simplify structure snapshots: rename table, drop LLM metadata, add ESC FK.

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-03-05 12:00:00.000000+00:00
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    # 1. Rename table
    op.rename_table("course_structure_snapshots", "structure_snapshots")

    # 2. Add FK to ExternalServiceCall (nullable — historical snapshots lack it)
    op.add_column(
        "structure_snapshots",
        sa.Column(
            "externalservicecall_id",
            sa.Uuid(),
            sa.ForeignKey("external_service_calls.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_structure_snapshots_externalservicecall_id",
        "structure_snapshots",
        ["externalservicecall_id"],
    )

    # 3. Data migration: create ExternalServiceCall for each existing snapshot
    # that has LLM metadata, then link via FK.
    op.execute(
        """
        WITH new_esc AS (
            INSERT INTO external_service_calls
                (id, action, strategy, provider, model_id, prompt_ref,
                 unit_type, unit_in, unit_out, cost_usd, success, created_at)
            SELECT
                gen_random_uuid(),
                'course_structuring',
                ss.mode,
                'unknown',
                COALESCE(ss.model_id, 'unknown'),
                ss.prompt_version,
                'tokens',
                ss.tokens_in,
                ss.tokens_out,
                ss.cost_usd,
                true,
                ss.created_at
            FROM structure_snapshots ss
            WHERE ss.model_id IS NOT NULL
               OR ss.tokens_in IS NOT NULL
            RETURNING id, created_at
        )
        UPDATE structure_snapshots ss
        SET externalservicecall_id = new_esc.id
        FROM new_esc
        WHERE ss.created_at = new_esc.created_at
          AND ss.externalservicecall_id IS NULL
        """
    )

    # 4. Drop duplicated LLM metadata columns
    op.drop_column("structure_snapshots", "prompt_version")
    op.drop_column("structure_snapshots", "model_id")
    op.drop_column("structure_snapshots", "tokens_in")
    op.drop_column("structure_snapshots", "tokens_out")
    op.drop_column("structure_snapshots", "cost_usd")

    # 5. Update unique index name (recreate with same columns, new table name)
    op.drop_index("uq_snapshots_identity", table_name="structure_snapshots")
    op.create_index(
        "uq_snapshots_identity",
        "structure_snapshots",
        [
            "course_id",
            sa.text("COALESCE(node_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "node_fingerprint",
            "mode",
        ],
        unique=True,
    )


def downgrade() -> None:
    # Re-add LLM metadata columns
    op.add_column(
        "structure_snapshots",
        sa.Column("prompt_version", sa.String(50), nullable=True),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column("model_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column("tokens_in", sa.Integer(), nullable=True),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column("tokens_out", sa.Integer(), nullable=True),
    )
    op.add_column(
        "structure_snapshots",
        sa.Column("cost_usd", sa.Float(), nullable=True),
    )

    # Copy data back from ESC
    op.execute(
        """
        UPDATE structure_snapshots ss
        SET model_id = esc.model_id,
            prompt_version = esc.prompt_ref,
            tokens_in = esc.unit_in,
            tokens_out = esc.unit_out,
            cost_usd = esc.cost_usd
        FROM external_service_calls esc
        WHERE ss.externalservicecall_id = esc.id
        """
    )

    # Drop ESC FK
    op.drop_index(
        "ix_structure_snapshots_externalservicecall_id",
        table_name="structure_snapshots",
    )
    op.drop_column("structure_snapshots", "externalservicecall_id")

    # Rename table back
    op.rename_table("structure_snapshots", "course_structure_snapshots")
