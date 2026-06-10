"""Phase 3.1 commit 4: server_default + NOT NULL on three content_hash columns.

Revision ID: phase31_content_hash_defaults
Revises: phase31_node_summary_tables
Create Date: 2026-06-10 00:00:01.000000

Closes the KD9 NULL-at-INSERT regression (vision §3 KD9 "Спостереження
Phase 1") + lands rule «визначений хеш, ніколи NULL» (Phase 3.1 Q-G)
atomically across the three hash-bearing tables that hold a methodist-
layer content hash:

* ``course_nodes.content_hash`` — backfill NULL → EMPTY_NODE_CONTENT_HASH,
  SET DEFAULT, SET NOT NULL.
* ``node_summaries_raw.content_hash`` — SET DEFAULT to
  EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH, SET NOT NULL (table created
  empty in commit 1; no backfill needed).
* ``node_summaries_final.content_hash`` — SET DEFAULT to
  EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH, SET NOT NULL (same).

Per Phase 2.4.13 ratified corrective + Q-F at 3.1 pre-flight: the
constants are hardcoded as string literals here rather than imported
from app code — replay safety. A repo-side assertion test
(``tests/storage/test_empty_hash_literals.py`` in commit 5) verifies
each literal equals the constant derived through the live
``compute_content_hash`` formula in ``storage/content_hash.py``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "phase31_content_hash_defaults"
down_revision: str | Sequence[str] | None = "phase31_node_summary_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Hardcoded per Phase 2.4.13 [[feedback-migration-self-contained-no-app-import]].
# Tests pin parity with the formula-derived constants in
# ``storage/content_hash.py`` so divergence surfaces immediately.
_EMPTY_NODE_CONTENT_HASH = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
_EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH = (
    "bcaf467defc6a11d9f437368b2388f310b8538fc2fe21621587f69175766cadd"
)
_EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH = (
    "85ef8d5f3881ffae484d1c1b47d18c0984b19c03bc9dd44e6fc1010e9fcdbf03"
)


def upgrade() -> None:
    """Backfill + SET DEFAULT + SET NOT NULL across three content_hash columns."""
    # CourseNode — backfill required: prior schema allowed NULL and
    # the Phase 1 regression left existing rows NULL until first PATCH.
    op.execute(
        sa.text(
            "UPDATE course_nodes SET content_hash = :hash WHERE content_hash IS NULL"
        ).bindparams(hash=_EMPTY_NODE_CONTENT_HASH)
    )
    op.alter_column(
        "course_nodes",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text(f"'{_EMPTY_NODE_CONTENT_HASH}'"),
    )

    # NodeSummaryRaw — created in commit 1, table empty, no backfill needed.
    op.alter_column(
        "node_summaries_raw",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text(f"'{_EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH}'"),
    )

    # NodeSummaryFinal — same.
    op.alter_column(
        "node_summaries_final",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text(f"'{_EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH}'"),
    )


def downgrade() -> None:
    """Revert to nullable, no default — preserves prior schema for replay."""
    op.alter_column(
        "node_summaries_final",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "node_summaries_raw",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "course_nodes",
        "content_hash",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )
