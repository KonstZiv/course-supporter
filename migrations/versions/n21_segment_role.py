"""№21 BE6: add document_segments.is_auxiliary boolean (file-role flag).

Revision ID: n21_segment_role
Revises: n21_prep_jobtype
Create Date: 2026-07-22 00:00:02.000000

№21 "typicality is not a property of the file", commit BE6 (role-routing). Adds
the boolean ``is_auxiliary`` role flag (decision 5) to ``document_segments`` —
the segment carrier the Mentor already reads for concepts, so role + concepts
arrive in one read. NOT NULL with ``server_default false``: legacy segments read
honestly as main/central (they predate roles), new rows carry the author's
decision. Written from the SAME author decision as the structure-tree role
(``document_summaries.structure``), test-locked to agree.

Downgrade drops the column (symmetric).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n21_segment_role"
down_revision: str | Sequence[str] | None = "n21_prep_jobtype"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the NOT NULL is_auxiliary boolean (server_default false)."""
    op.add_column(
        "document_segments",
        sa.Column(
            "is_auxiliary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "№21 (decision 5): the file-role role of this code segment — "
                "True = auxiliary (its concepts merge into the material's "
                "secondary only), False = the central/main role (concepts "
                "routed as-is). server_default false so legacy segments read "
                "honestly as main. Written from the SAME author decision as the "
                "structure-tree role in one moment (invariant, test-locked)."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the is_auxiliary column."""
    op.drop_column("document_segments", "is_auxiliary")
