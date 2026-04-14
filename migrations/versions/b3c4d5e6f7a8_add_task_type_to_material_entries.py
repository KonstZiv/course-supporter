"""add_task_type_to_material_entries

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-14 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add task_type enum and nullable column to material_entries.

    task_type marks a material as containing a concrete assignment. When set,
    the MethodistAgent must preserve the material as a recommended assignment
    instead of generating one from scratch. The enum mirrors AssignmentType
    in models/methodist.py.
    """
    op.execute(
        "CREATE TYPE task_type_enum AS ENUM ('test', 'short_task', 'task', 'project')"
    )
    op.add_column(
        "material_entries",
        sa.Column(
            "task_type",
            sa.Enum(
                "test",
                "short_task",
                "task",
                "project",
                name="task_type_enum",
                create_type=False,
            ),
            nullable=True,
            comment=(
                "Assignment type if this material represents a concrete task "
                "(test, short_task, task, project). NULL means regular material."
            ),
        ),
    )


def downgrade() -> None:
    """Remove task_type column and enum."""
    op.drop_column("material_entries", "task_type")
    op.execute("DROP TYPE IF EXISTS task_type_enum")
