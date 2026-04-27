"""0.2 content hash infrastructure: content_hash columns + raw_hash index

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-04-27 14:35:00.000000

Task 0.2 of the Phase 0 sprint (vision §3 KD9). Adds the
materialised KD9 ``content_hash`` on every node of the material
tree plus a lookup index on the existing ``MaterialEntry.raw_hash``:

- ``content_hash TEXT NULL`` on ``material_entries``,
  ``material_macro_sections``, ``material_segments``, and
  ``material_nodes``. Lowercase hex SHA-256 string. ``NULL`` =
  "never computed / stale"; population happens at INSERT/UPDATE
  time per vision §3 KD9 line 563 (no lazy / on-read
  materialisation). Backfill on existing rows is intentionally
  out-of-scope — production data is empty (KD0).
- ``ix_material_entries_raw_hash`` index for re-upload detection
  (vision §3 KD4 + KD9). Regular (non-unique) index — same content
  uploaded twice is intentionally allowed in v0.20.

The legacy ``MaterialNode.node_fingerprint`` column is left in
place. Phase 1.1 (CourseNode rename) collapses it into
``content_hash`` together with the formula change to KD9 inputs
(``raw_hash`` + associated ``DocumentSummary.content_hash`` +
child ``CourseNode.content_hash`` values), and migrates the five
legacy read-paths (snapshot identity, reconciliation cache,
enqueue, generation idempotency, repository invalidation) to the
new column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONTENT_HASH_TABLES: tuple[str, ...] = (
    "material_entries",
    "material_macro_sections",
    "material_segments",
    "material_nodes",
)

CONTENT_HASH_COMMENT = (
    "Materialised content_hash (vision §3 KD9, SHA-256 hex). "
    "NULL = never computed / stale. Populated at INSERT/UPDATE; "
    "per-entity formula wired in Phase 2/3."
)


def upgrade() -> None:
    """Add content_hash on 4 tables + raw_hash lookup index."""
    for table in CONTENT_HASH_TABLES:
        op.add_column(
            table,
            sa.Column(
                "content_hash",
                sa.String(64),
                nullable=True,
                comment=CONTENT_HASH_COMMENT,
            ),
        )

    op.create_index(
        "ix_material_entries_raw_hash",
        "material_entries",
        ["raw_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Drop raw_hash index + content_hash columns."""
    op.drop_index("ix_material_entries_raw_hash", table_name="material_entries")
    for table in CONTENT_HASH_TABLES:
        op.drop_column(table, "content_hash")
