"""Phase 6 T3: persisted per-slide WebP keys on authored_documents.

Revision ID: portal_t3_slide_keys
Revises: portal_t2_delivery_mode
Create Date: 2026-06-26 00:00:00.000000

Adds the carrier for the student-portal media layer (KD17 §3): the ordered
S3 object keys of the per-slide WebP renders that the ``arq_ingest_material``
seam persists after Pass 2b.

  - ``authored_documents.slide_keys`` — nullable JSONB array of S3 keys
    (index = slide_number - 1). NULL for non-presentation sources and for
    presentations ingested before T3; a populated list means the slides are
    addressable via the portal media endpoint.

Forward-only: nullable with no server_default and no backfill — existing
presentations keep ``slide_keys = NULL`` and gain their slides on re-ingest
(Deployment action T3, mirroring the 3.3a forward-only precedent). The
downgrade drops the column for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "portal_t3_slide_keys"
down_revision = "portal_t2_delivery_mode"
branch_labels = None
depends_on = None

_AUTHORED = "authored_documents"


def upgrade() -> None:
    """Add the nullable slide_keys JSONB column (forward-only, no backfill)."""
    op.add_column(
        _AUTHORED,
        sa.Column(
            "slide_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Phase 6 T3 (KD17): ordered S3 object keys of the persisted "
                "per-slide WebP renders (index = slide_number - 1). NULL for "
                "non-presentation sources and presentations ingested before "
                "T3; a populated list means the slides are addressable via "
                "the portal media endpoint. Written on the arq_ingest_material "
                "seam after Pass 2b, overwritten in place on re-ingest. "
                "Gathered into the delete-cleanup key set so the WebP objects "
                "are scrubbed with the document. NOT in content_hash."
            ),
        ),
    )


def downgrade() -> None:
    """Reverse: drop the column (round-trip only)."""
    op.drop_column(_AUTHORED, "slide_keys")
