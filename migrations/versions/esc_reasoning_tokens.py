"""STEP-0 P5/P6: add external_service_calls.unit_out_reasoning (nullable).

Revision ID: esc_reasoning_tokens
Revises: n21_segment_role
Create Date: 2026-07-28 00:00:00.000000

Reasoning-token visibility for the accounting log (STEP-0 connector-fix
package). Adds the nullable ``unit_out_reasoning`` Integer column to
``external_service_calls`` — a subset of ``unit_out`` the DashScope connector
now extracts from ``usage.reasoning_tokens``. NULLABLE with NO backfill: the
column is unknown for every historical row (the connector did not read it
before), and NULL vs 0 is a meaningful distinction (provider did not report vs
reported zero) that a server_default would erase.

Downgrade drops the column (symmetric).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "esc_reasoning_tokens"
down_revision: str | Sequence[str] | None = "n21_segment_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable unit_out_reasoning column (no backfill)."""
    op.add_column(
        "external_service_calls",
        sa.Column(
            "unit_out_reasoning",
            sa.Integer(),
            nullable=True,
            comment=(
                "Reasoning tokens billed as a subset of unit_out (STEP-0 P5/P6 "
                "accounting visibility). NULL = provider did not report; 0 = "
                "reported zero. DashScope reads usage.reasoning_tokens; other "
                "providers leave it NULL."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the unit_out_reasoning column."""
    op.drop_column("external_service_calls", "unit_out_reasoning")
