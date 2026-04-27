"""0.2 review fix: convert ix_material_entries_raw_hash to a partial index

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-04-27 16:45:00.000000

Reviewer follow-up to task 0.2 (vision §3 KD9). The
``ix_material_entries_raw_hash`` index added in
``c0d1e2f3a4b5`` covered every row, but the only consumer
(``MaterialEntryRepository.find_by_raw_hash``) always filters
``deleted_at IS NULL`` — soft-deleted entries are never re-upload
candidates by definition (their authored content is scrubbed,
KD3). A partial index over the same predicate gives a smaller
on-disk footprint and lets PostgreSQL use index-only scans for
the lookup. Mirrors the ``ix_material_entries_active`` pattern
introduced in 0.1, where every soft-deletable table got a
``WHERE deleted_at IS NULL`` partial index for cheap "active-only"
queries.

Implemented as a drop + recreate (one round-trip, on a
just-introduced index that no production caller has hit yet) rather
than an in-place edit of ``c0d1e2f3a4b5`` so any local dev DB that
already ran the original migration upgrades cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the full-table raw_hash index with a partial one."""
    op.drop_index("ix_material_entries_raw_hash", table_name="material_entries")
    op.create_index(
        "ix_material_entries_raw_hash",
        "material_entries",
        ["raw_hash"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Restore the full-table (non-partial) index."""
    op.drop_index("ix_material_entries_raw_hash", table_name="material_entries")
    op.create_index(
        "ix_material_entries_raw_hash",
        "material_entries",
        ["raw_hash"],
        unique=False,
    )
