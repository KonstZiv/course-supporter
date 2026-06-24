"""sprint-mentor T4: cached task→criteria decomposition (D11).

Revision ID: mentor_t4_criteria_cache
Revises: mentor_t3_review_signals
Create Date: 2026-06-24 00:00:00.000000

Creates ``task_criteria`` — the Raw-equivalent cache of a task's checkable
review criteria (vision §1386, D11). One ACTIVE row per task version; the
Mentor review graph (T6) reuses it read-through so the per-submission review
does not re-derive the criteria. Mirrors the NodeSummaryRaw cache apparatus
(KD11):

- ``authored_document_id`` — FK to the task AuthoredDocument (ondelete CASCADE
  for hard-delete; soft-delete routes through the cascade service).
- ``criteria`` — JSONB list of {statement, evidence}.
- ``source_content_hash`` — content-axis version key (= AuthoredDocument
  .content_hash at compute time).
- ``source_task_type`` — type-axis version key (= AuthoredDocument.task_type
  at compute time; the decomposition is task_type-aware).

A partial-active unique on ``authored_document_id`` enforces the one-active-row
invariant while soft-deleted history coexists (release-old/insert-new on
re-version, mirroring DocumentSummary, Task 3.2.6). No BEFORE-UPDATE soft-delete
trigger is added — post-0.1 soft-deletable tables (node_summaries) follow the
same convention: the partial-active index plus repository-level idempotency are
the guards.

Forward-only: brand-new table, no backfill (homework not yet live on prod).
The downgrade drops the table for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "mentor_t4_criteria_cache"
down_revision = "mentor_t3_review_signals"
branch_labels = None
depends_on = None

_TABLE = "task_criteria"


def upgrade() -> None:
    """Create task_criteria + its active / partial-active-unique indexes."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            sa.ForeignKey("authored_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK to the task AuthoredDocument (active-only 1:1 invariant per "
                "uq_task_criteria_authored_document_id_active). Hard-delete "
                "cascades with the document; soft-delete cascades via "
                "AuthoredDocument.__cascades_soft_delete_to__."
            ),
        ),
        sa.Column(
            "criteria",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment=(
                "The decomposition: list of checkable criteria, each "
                "{statement, evidence}. Non-empty for a materialised row "
                "(semantic minimum enforced at compute time)."
            ),
        ),
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Content-axis version key = AuthoredDocument.content_hash at "
                "compute time (mirrors NodeSummaryRaw.source_content_hash). A "
                "reuse hit requires this to equal the document's current "
                "content_hash."
            ),
        ),
        sa.Column(
            "source_task_type",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Type-axis version key = AuthoredDocument.task_type at compute "
                "time. The decomposition is task_type-aware, so re-typing the "
                "task invalidates the cache alongside a content change."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Soft-delete timestamp (vision KD3). "
                "NULL = active; NOT NULL = soft-deleted."
            ),
        ),
        comment=(
            "Cached task→criteria decomposition (vision §1386, D11) — one "
            "ACTIVE row per task version; reused read-through by the Mentor "
            "review graph (T6)"
        ),
    )
    op.create_index(
        "ix_task_criteria_authored_document_id",
        _TABLE,
        ["authored_document_id"],
    )
    op.create_index(
        "ix_task_criteria_active",
        _TABLE,
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_task_criteria_authored_document_id_active",
        _TABLE,
        ["authored_document_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Drop task_criteria (round-trip only)."""
    op.drop_index("uq_task_criteria_authored_document_id_active", table_name=_TABLE)
    op.drop_index("ix_task_criteria_active", table_name=_TABLE)
    op.drop_index("ix_task_criteria_authored_document_id", table_name=_TABLE)
    op.drop_table(_TABLE)
