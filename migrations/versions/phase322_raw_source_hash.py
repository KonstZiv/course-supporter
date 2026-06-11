"""Phase 3.2.2 commit 1: NodeSummaryRaw.source_content_hash (bottom-up memo key).

Revision ID: phase322_raw_source_hash
Revises: phase31_content_hash_defaults
Create Date: 2026-06-11 00:00:00.000000

Adds the second axis-1 hash linkage column to ``node_summaries_raw`` per
vision §3 KD10 line 1011 (bottom-up memoization key = the source
``CourseNode.content_hash`` at the time the Raw was generated, NOT the
Raw's own self-hash).

Phase 3.1 shipped ``NodeSummaryRaw.content_hash`` as a self-hash over
own content fields (KD9 line 729). KD10 line 1011 separately requires a
**source linkage** column to memo-skip Pass 1 LLM calls when the
source CourseNode has not changed. These two roles are semantically
distinct and need two columns; this migration adds the second.

Column shape:

* ``String(64)`` — same width as the other SHA-256-hex columns.
* ``nullable=True`` — written by the Phase 3.2.2 orchestrator only
  after a successful Pass 1 visit completes (the LLM hook in skeleton
  3.2.2 is a no-op, but the orchestrator still materialises the value
  in the post-hook commit so memo-skip works across reruns). NULL is
  the legitimate "Raw row exists, Pass 1 has not yet completed against
  any source" state — see Phase 3.2.2 task spec acceptance #3/#6 for
  why this two-phase write is required.
* No ``server_default`` — NULL is the meaningful initial state
  (mirrors ``enclosing_context_source_hash`` from Phase 3.1).

No backfill: ``node_summaries_raw`` is empty in production (no writer
exists yet; Phase 3.2.2 orchestrator is the first one). Even in
dev/test environments where the table is empty, no rows need a
backfilled value — the NULL semantics correctly mean "not yet
generated".

Self-contained per [[feedback-migration-self-contained-no-app-import]] —
no app-code imports.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "phase322_raw_source_hash"
down_revision: str | Sequence[str] | None = "phase31_content_hash_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``node_summaries_raw.source_content_hash`` (nullable, no default)."""
    op.add_column(
        "node_summaries_raw",
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=True,
            comment=(
                "CourseNode.content_hash at the time Pass 1 last completed — "
                "bottom-up memo key (vision §3 KD10 line 1011). NULL until "
                "first Pass 1 lands; written by the Phase 3.2.2 orchestrator "
                "after the LLM hook commits per-node."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the column. Replay-safe: prior schema had no such column."""
    op.drop_column("node_summaries_raw", "source_content_hash")
