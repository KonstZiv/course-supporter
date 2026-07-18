"""L4: drop dead ``jobs.depends_on`` + resync two fossil column comments.

Revision ID: l4_drop_depends_on
Revises: l2_obsolete_status
Create Date: 2026-07-18 00:00:00.000000

Job-lifecycle contract, step L4 (cleanup), commit 1. Three column-level changes
on ``jobs``:

1. Drop ``depends_on`` (JSONB). The dependent-job cascade it fed
   (``propagate_failure`` / ``_find_dependents``) had NO writer anywhere in the
   codebase (all five ``JobRepository.create`` call-sites pass the default;
   no direct assignment; no migration backfill) and NO non-empty row in prod or
   dev (gate P1(b): ``jsonb_typeof(depends_on)='array' AND depends_on <> '[]'``
   → 0). KD13 collapsed multi-job orchestration into one-Job-with-stages; the
   column is a fossil of the retired model.

2. Resync ``result_data`` comment. The stored DB comment named
   ``reconciliation preview issues`` — a DELETED entity (ReconciliationPreview);
   the column now carries the task body's return dict via the execution seam's
   ``store_result``.

3. Resync ``course_node_id`` comment. The stored DB comment predates the Phase
   1.1 rename (``MaterialNode`` → ``CourseNode``) and the L1b subject move; it
   was updated in the ORM across phases but never migrated. The column stays
   (live cost-attribution readers, contract FORBIDDEN); only the comment moves.

``existing_comment`` on both ALTERs is the BYTE-EXACT stored DB value
(pre-flight capture 2026-07-18; corroborated against the create-comments in
``f7b8d706ad9c_initial_schema_9_tables``), NOT the drifted ORM text — so the
downgrade restores the true prior DB state, not an assumption.

Downgrade re-adds ``depends_on`` as a nullable JSONB with its byte-exact old DB
comment and reverts both comments to their stored DB values. Round-trip is
data-lossless ON the proven invariant "zero array rows": there is no dependency
data to preserve. Dev rows that historically stored the JSON ``null`` literal
(not SQL NULL) normalise to SQL NULL after drop + re-add — a semantic no-op
(neither carries a dependency), not a loss. No index / constraint / FK
references ``depends_on`` (``ck_jobs_status`` / ``uq_jobs_subject_in_flight`` /
the subject CHECKs are untouched), so this is a clean single-column drop.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l4_drop_depends_on"
down_revision: str | Sequence[str] | None = "l2_obsolete_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"

# Byte-exact stored DB comments (pre-flight 2026-07-18) — the ``existing_comment``
# side of each ALTER. NOT the ORM text: for result_data the two happen to match;
# for course_node_id the DB carries the pre-Phase-1.1 "MaterialNode" wording.
_RESULT_DATA_DB = "JSONB result payload (e.g. reconciliation preview issues)"
_COURSE_NODE_DB = "FK to target MaterialNode. NULL for orphaned jobs"
_DEPENDS_ON_DB = "JSONB array of Job UUIDs that must complete first"

# New comments — byte-exact mirror of the post-L4 ORM ``comment=`` strings.
_RESULT_DATA_NEW = (
    "JSONB result payload — the task body's return dict, persisted by the "
    "execution seam via store_result. Concretely: s3_cleanup → {deleted: "
    "[key…], errors: [{key, error}…]}. NULL when the body returns nothing."
)
_COURSE_NODE_NEW = (
    "FK to target CourseNode (legacy table name course_nodes until Phase 1.1 "
    "rename). NULL for orphaned jobs. L1b: no longer the cancellation target "
    "(that is subject_id) — context/attribution only. Live readers: cost "
    "attribution (cost summary). No planned drop."
)


def upgrade() -> None:
    """Drop ``depends_on``; resync ``result_data`` / ``course_node_id`` comments."""
    op.drop_column(_JOBS, "depends_on")
    op.alter_column(
        _JOBS,
        "result_data",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        comment=_RESULT_DATA_NEW,
        existing_comment=_RESULT_DATA_DB,
    )
    op.alter_column(
        _JOBS,
        "course_node_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        comment=_COURSE_NODE_NEW,
        existing_comment=_COURSE_NODE_DB,
    )


def downgrade() -> None:
    """Re-add ``depends_on`` (nullable, old DB comment); revert both comments."""
    op.alter_column(
        _JOBS,
        "course_node_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        comment=_COURSE_NODE_DB,
        existing_comment=_COURSE_NODE_NEW,
    )
    op.alter_column(
        _JOBS,
        "result_data",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        comment=_RESULT_DATA_DB,
        existing_comment=_RESULT_DATA_NEW,
    )
    op.add_column(
        _JOBS,
        sa.Column(
            "depends_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=_DEPENDS_ON_DB,
        ),
    )
