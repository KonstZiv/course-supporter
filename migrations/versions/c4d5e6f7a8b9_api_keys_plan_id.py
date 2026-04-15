"""api_keys: replace rate_limit_prep/check with plan_id

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-15 10:00:00.000000

Moves per-key rate limits out of the database into config/auth.yaml.
Each APIKey now points to a named plan (default: 'basic') and the
scope-enforcement layer resolves the effective limit from the plan
registry at request time.

Data migration: server_default='basic' backfills plan_id for every
existing row. Prod audit at deploy time showed a single key with
(60, 300), which matches the 'basic' plan exactly — no manual UPDATE
required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add plan_id, drop rate_limit_prep/check."""
    op.add_column(
        "api_keys",
        sa.Column(
            "plan_id",
            sa.String(length=50),
            nullable=False,
            server_default="basic",
            comment=(
                "Name of plan from config/auth.yaml — "
                "resolves to per-scope rate limits at runtime."
            ),
        ),
    )
    op.drop_column("api_keys", "rate_limit_prep")
    op.drop_column("api_keys", "rate_limit_check")


def downgrade() -> None:
    """Restore rate_limit_prep/check columns with prior defaults."""
    op.add_column(
        "api_keys",
        sa.Column(
            "rate_limit_prep",
            sa.Integer(),
            nullable=False,
            server_default="60",
            comment="Max requests per 60s window for prep scope",
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "rate_limit_check",
            sa.Integer(),
            nullable=False,
            server_default="300",
            comment="Max requests per 60s window for check scope",
        ),
    )
    op.drop_column("api_keys", "plan_id")
