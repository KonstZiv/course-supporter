"""№21 BE2: add authored_documents.file_roles JSONB column

Revision ID: n21_authored_file_roles
Revises: l4_drop_depends_on
Create Date: 2026-07-22 00:00:00.000000

№21 "typicality is not a property of the file", commit BE2 (file-roles-column).
Adds a nullable ``file_roles`` JSONB column on ``authored_documents`` to carry
the author's file-role markup: input on the document row, output stays in
``document_summaries.structure``.

Shape (KD20 proposal/decision separation) is two independent top-level keys,
mirrored in the ORM column comment:

* ``proposal`` — ``{files: {<path>: {role, reason}}, tree_digest, computed_at}``;
  written ONLY by the DOCUMENT_PREPARATION job (a later commit).
* ``decision`` — ``{files: {<path>: <role>}, tree_digest, decided_at}`` or
  absent; written ONLY by the confirm endpoint (BE5). Writing ``decision``
  never mutates ``proposal`` (invariant I1) — they are separate keys so the
  proposal-vs-decision delta survives as labelled data.

Roles: ``full`` | ``auxiliary`` | ``structure_only``. Column stays NULL until
the prep job has run (all pre-№21 rows keep NULL). Greenfield column, no
backfill; downgrade drops it (lossless round-trip).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "n21_authored_file_roles"
down_revision: str | Sequence[str] | None = "l4_drop_depends_on"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable file_roles JSONB column to authored_documents."""
    op.add_column(
        "authored_documents",
        sa.Column(
            "file_roles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "№21 author file-role markup (KD20 proposal/decision "
                "separation). Two independent top-level keys: 'proposal' "
                "(system suggestion — {files: {<path>: {role, reason}}, "
                "tree_digest, computed_at}, written ONLY by the "
                "DOCUMENT_PREPARATION job) and 'decision' (author's "
                "confirmation — {files: {<path>: <role>}, tree_digest, "
                "decided_at}, written ONLY by the confirm endpoint) or absent. "
                "Roles: 'full' | 'auxiliary' | 'structure_only'. Writing "
                "'decision' NEVER mutates 'proposal' (invariant I1 / KD20): "
                "separate keys, so the proposal-vs-decision delta survives as "
                "labelled data. NULL until the prep job has run."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the file_roles column."""
    op.drop_column("authored_documents", "file_roles")
