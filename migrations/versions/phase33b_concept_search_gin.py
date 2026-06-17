"""Phase 3.3b: declare KD-gamma concept-search GIN indexes in the model.

Revision ID: phase33b_concept_search_gin
Revises: phase33a_segment_anchors
Create Date: 2026-06-17 00:00:00.000000

Phase 3.3b (concept-search) declares the two KD-gamma GIN indexes in
``DocumentSegment.__table_args__``::

    ix_document_segments_main_concepts_gin       (main_concepts jsonb_path_ops)
    ix_document_segments_secondary_concepts_gin  (secondary_concepts jsonb_path_ops)

These indexes already EXIST in every database — they were created by raw
``op.execute("CREATE INDEX ... USING gin (... jsonb_path_ops)")`` in the
phase-1 baseline migration ``a1b2c3d4e5f6``. The ORM model simply never
declared them, so autogenerate could not see them on the model side
(a standing model↔DB drift, now closed).

Round-trip result — PROVEN, not assumed (the §6 CAVEAT: Alembic historically
under-introspects opclass). With the declaration in place,
``alembic revision --autogenerate`` emits NO operation for either GIN index:
Alembic does not introspect the ``jsonb_path_ops`` opclass, so a declared GIN
index whose name already exists in the DB yields an empty diff. This
migration is therefore intentionally a no-op — the declaration closes the
model-side drift; there is no DDL to run because the DB already matches.

Scope note: the same autogenerate run also surfaced large, UNRELATED,
pre-existing schema drift — table/column comment churn across many tables
(legacy "MaterialNode"/"MaterialEntry" wording) and a unique-constraint vs
unique-index mismatch on ``node_summaries_raw`` / ``node_summaries_final``.
None of it is caused by Phase 3.3b; it is out of scope and deliberately NOT
folded in here. Recorded as a standing-drift forward-note for vision-side.
"""

from __future__ import annotations

revision = "phase33b_concept_search_gin"
down_revision = "phase33a_segment_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: the KD-gamma GIN indexes already exist (baseline a1b2c3d4e5f6).

    The Phase 3.3b deliverable is the model declaration in
    ``DocumentSegment.__table_args__``; there is no DDL to apply.
    """


def downgrade() -> None:
    """No-op: nothing was created on upgrade (see the upgrade docstring)."""
