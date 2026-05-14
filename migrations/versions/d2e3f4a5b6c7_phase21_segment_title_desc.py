"""Phase 2.1 C7: add document_segments.title + description columns

Revision ID: phase21_segment_title_desc
Revises: phase21_authored_safety_result
Create Date: 2026-05-12 00:00:00.000000

Adds two nullable text columns to ``document_segments`` to persist
Pass 2a-emitted per-segment heading and description (KD-2.1-O carries
them through DocumentSegmentDraft into the ORM row at Pass 2b).

Rationale for nullable: ``DocumentSegment`` is the canonical contract
for all source_type. Pass 2a (text/web) populates both columns at C7;
audio/video pipelines synthesise title/description in later phases
(post-Pass-2c) and legitimately leave them NULL until then. Storing
sentinels (empty strings) would mask the transient state.

Both columns are intentionally excluded from the
``DocumentSegment.content_hash`` formula in C7 -- see DD-2.1-W
(Phase 1.3 / 1.4 hash formula refresh) for the bundled fix together
with ``positions`` field inclusion (DD-1.1-3).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "phase21_segment_title_desc"
down_revision: str | Sequence[str] | None = "phase21_authored_safety_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable title + description columns to document_segments."""
    op.add_column(
        "document_segments",
        sa.Column(
            "title",
            sa.String(length=128),
            nullable=True,
            comment=(
                "Optional short heading (Pass 2a LLM emits per segment for "
                "text/web; NULL for audio/video where title is synthesised "
                "later). NOT in content_hash formula -- DD-2.1-W defers to "
                "Phase 1.3/1.4."
            ),
        ),
    )
    op.add_column(
        "document_segments",
        sa.Column(
            "description",
            sa.String(length=512),
            nullable=True,
            comment=(
                "1-2 sentence segment description (Pass 2a LLM emits per "
                "segment; populated for text/web at C7, NULL for media "
                "pre-Pass-2c). NOT in content_hash formula -- DD-2.1-W "
                "defers to Phase 1.3/1.4."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the title + description columns."""
    op.drop_column("document_segments", "description")
    op.drop_column("document_segments", "title")
