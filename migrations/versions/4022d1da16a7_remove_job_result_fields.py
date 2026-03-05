"""remove result_material_id, result_snapshot_id, chk_job_result_exclusive

Revision ID: 4022d1da16a7
Revises: 63c6565a0e1b
Create Date: 2026-03-05 14:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4022d1da16a7"
down_revision: Union[str, None] = "63c6565a0e1b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("chk_job_result_exclusive", "jobs", type_="check")
    op.drop_column("jobs", "result_material_id")
    op.drop_column("jobs", "result_snapshot_id")


def downgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("result_material_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("result_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.create_check_constraint(
        "chk_job_result_exclusive",
        "jobs",
        "NOT (result_material_id IS NOT NULL AND result_snapshot_id IS NOT NULL)",
    )
