"""task-code-materials commit 4: four nullable schema columns.

Revision ID: codemat_schema_columns
Revises: codemat_source_type
Create Date: 2026-07-12 00:00:00.000000

One revision, four nullable ADD COLUMNs (ratified contract — separate
from the enum ALTER in ``codemat_source_type``):

* ``document_segments.file_path`` — fourth positional-anchor kind
  (single column, not a start/end pair: one included file = one
  segment). Extends the phase33a anchor convention "exactly one
  anchor non-null per source_type"; NULL for every non-code row.
* ``document_summaries.structure`` — code-material project tree
  (path / cls / size + exclusion reason), populated by code from the
  extraction manifest. NULL for every non-code row.
* ``jobs.error_category`` + ``authored_documents.error_category`` —
  structural async-error code (F4 / DD-2.3-AH): the security
  ``ErrorCategory`` ``.value`` stored as a plain string (no PG enum —
  avoids the non-droppable-enum trap); validated at the write site.

All columns are nullable with no server_default and no backfill
(forward-only, phase33a precedent) — backward-safe for existing rows.

Downgrade is a FULL structural round-trip over these four columns
(project_bases_p2 precedent). NOTE: the ``'code'`` value added to
``source_type_enum`` by the PREVIOUS revision (``codemat_source_type``)
intentionally survives any downgrade — PostgreSQL cannot drop an enum
value without recreating the type; that revision's downgrade raises
``NotImplementedError`` per the phase22 audio precedent, accepted with
an explicit flag in the ratified task contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "codemat_schema_columns"
down_revision: str | Sequence[str] | None = "codemat_source_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the four nullable task-code-materials columns."""
    op.add_column(
        "document_segments",
        sa.Column(
            "file_path",
            sa.Text(),
            nullable=True,
            comment=(
                "Code segment source-file path, canonical POSIX relative "
                "(task-code-materials; one included file = one segment, so "
                "a single column — not a start/end pair). NULL for every "
                "non-code source_type. Deliberately NOT in content_hash."
            ),
        ),
    )
    op.add_column(
        "document_summaries",
        sa.Column(
            "structure",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Code-material project tree (path / cls / size, exclusion "
                "reason for typical files), populated by code from the "
                "extraction manifest. NULL for non-code source types."
            ),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "error_category",
            sa.String(length=50),
            nullable=True,
            comment=(
                "Structural async-error code (task-code-materials F4): "
                "security ErrorCategory .value as a plain string; "
                "validated at the write site. NULL = uncategorised."
            ),
        ),
    )
    op.add_column(
        "authored_documents",
        sa.Column(
            "error_category",
            sa.String(length=50),
            nullable=True,
            comment=(
                "Structural async-error code (task-code-materials F4): "
                "security ErrorCategory .value as a plain string; "
                "validated at the write site. NULL = uncategorised."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the four columns (full round-trip; enum value survives — see module docstring)."""
    op.drop_column("authored_documents", "error_category")
    op.drop_column("jobs", "error_category")
    op.drop_column("document_summaries", "structure")
    op.drop_column("document_segments", "file_path")
