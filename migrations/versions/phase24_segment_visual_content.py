"""Phase 2.4 task 2.4.6: add document_segments.visual_content JSONB column

Revision ID: phase24_segment_visual_content
Revises: phase22_audio_source_type
Create Date: 2026-05-23 00:00:00.000000

Adds the ``visual_content`` JSONB column to ``document_segments`` to
persist a video segment's time-anchored visual stream (Pass 1 frame
descriptions partitioned over the segment timeline at Pass 2b). The
visual stream is genuine authored content for video and is persisted
nowhere else, so an ORM column is the only place it survives ingestion
(task 2.4.6 §1).

Non-expansive / backward-compatible: not-null with ``server_default
'[]'::jsonb`` so every existing row backfills to an empty array and no
caller of another source type changes. Only the video pipeline writes a
non-empty value. Mirrors the ``main_concepts`` / ``secondary_concepts``
JSONB + server_default mechanics on the same table.

The column joins the ``DocumentSegment.content_hash`` formula (KD-2.1-F)
CONDITIONALLY (only when non-empty) so historical and non-video segment
hashes stay byte-identical -- see ``storage/content_hash.py`` and task
2.4.6 D1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "phase24_segment_visual_content"
down_revision: str | Sequence[str] | None = "phase22_audio_source_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the not-null visual_content JSONB column (default empty array)."""
    op.add_column(
        "document_segments",
        sa.Column(
            "visual_content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment=(
                "Time-anchored visual descriptions for video segments "
                "(task 2.4.6): JSONB array of VisualSceneRef dicts "
                "(position_ms / description / kind / scene_id), kept in "
                "temporal (frame) order. Empty [] for non-video and "
                "visual-less segments. Mirrors the main_concepts "
                "JSONB+server_default mechanics; included in content_hash "
                "(KD-2.1-F) only when non-empty (task 2.4.6 D1)."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the visual_content column."""
    op.drop_column("document_segments", "visual_content")
