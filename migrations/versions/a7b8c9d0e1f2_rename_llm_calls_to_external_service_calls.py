"""rename llm_calls → external_service_calls, rename columns, add job_id + unit_type

Revision ID: a7b8c9d0e1f2
Revises: 4022d1da16a7
Create Date: 2026-03-05 20:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "4022d1da16a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename table
    op.rename_table("llm_calls", "external_service_calls")

    # 2. Rename columns
    op.alter_column("external_service_calls", "tokens_in", new_column_name="unit_in")
    op.alter_column("external_service_calls", "tokens_out", new_column_name="unit_out")
    op.alter_column(
        "external_service_calls", "prompt_version", new_column_name="prompt_ref"
    )

    # 3. Add new columns
    op.add_column(
        "external_service_calls",
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    op.add_column(
        "external_service_calls",
        sa.Column("unit_type", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    # Drop new columns
    op.drop_column("external_service_calls", "unit_type")
    op.drop_column("external_service_calls", "job_id")

    # Rename columns back
    op.alter_column(
        "external_service_calls", "prompt_ref", new_column_name="prompt_version"
    )
    op.alter_column("external_service_calls", "unit_out", new_column_name="tokens_out")
    op.alter_column("external_service_calls", "unit_in", new_column_name="tokens_in")

    # Rename table back
    op.rename_table("external_service_calls", "llm_calls")
