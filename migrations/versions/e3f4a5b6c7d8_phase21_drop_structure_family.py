"""Phase 2.1 C9.4: drop Phase 5 Structure family + ReconciliationPreview tables.

Revision ID: phase21_drop_structure_family
Revises: phase21_segment_title_desc
Create Date: 2026-05-13 00:00:00.000000

Drops the four Phase 5 tables that were structurally retired by the
C9 cleanup chain (commits 239393b → 665ec69):

- ``structure_nodes_editable`` — methodist editable overlay (Phase 5.2).
- ``structure_nodes`` — generated structure tree (Phase 5.1).
- ``structure_snapshots`` — frozen snapshots of generated structure.
- ``reconciliation_previews`` — cross-node consistency LLM cache.

Production code for the consumers of these tables (4 agents +
4 orchestrators + 4 routes + 5 storage repos + assorted helpers
totalling ~6900 LOC) was removed at C9.3 (commit ``665ec69``).
ORM classes (StructureNode / StructureNodeEditable / StructureSnapshot
/ ReconciliationPreview) + the two ``CourseNode`` relationships
that targeted them (``snapshots``, ``editables``) + supporting enums
(``StructureNodeType``, ``GenerationMode``, ``MacroSectionStatus``)
were removed in the same commit as this migration (C9.4).

FK-order rationale (FK graph is internal *except* for the
``homework_submissions.matched_task_id`` reference into
``structure_nodes_editable``, which is the DD-2.1-AG retained orphan
column — its ORM-side FK declaration was dropped in this same commit,
but the live DB still carries the underlying ``ForeignKeyConstraint``
that the autogenerate-was-not-used path leaves intact). The constraint
is dropped explicitly **before** ``structure_nodes_editable`` to avoid
``DependentObjectsStillExist`` on the table drop:

- ``homework_submissions.matched_task_id_fkey`` → drop first
  (DD-2.1-AG: column retained, FK retired with the target table).
- ``structure_nodes_editable`` has FKs to ``structure_nodes``,
  ``structure_snapshots``, ``course_nodes``, ``external_service_calls``,
  self-FK on ``parent_editable_id``.
- ``structure_nodes`` has FK to ``structure_snapshots`` and self-FK
  on ``parent_structurenode_id``.
- ``structure_snapshots`` has FKs to ``course_nodes`` and
  ``external_service_calls``.
- ``reconciliation_previews`` has FKs to ``course_nodes`` and ``jobs``.

PostgreSQL ``DROP TABLE`` automatically cascades to dependent indexes,
unique constraints, and check constraints, so the migration body is
intentionally concise beyond the one explicit ``drop_constraint`` —
``drop_index`` calls would only duplicate what the engine already does.

Downgrade is intentionally NOT implemented per the "no speculative
rollback" principle — Phase 5 production code (~6900 LOC) was removed
in the same C9 chain; a reverse migration that recreates these tables
would leave them orphaned. Phase 3 (KD6/KD10/KD11) will introduce
``node_summary_raw`` and ``node_summary_final`` greenfield tables as
the conceptual successor; that is a redesign, not a column-level
reverse of this migration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "phase21_drop_structure_family"
down_revision: str | Sequence[str] | None = "phase21_segment_title_desc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the four Phase 5 tables in FK-safe order.

    Drops the surviving ``homework_submissions.matched_task_id`` FK
    (DD-2.1-AG: column retained as an orphan, FK retired with the
    target table) before the table itself to satisfy PostgreSQL
    referential-integrity ordering.
    """
    op.drop_constraint(
        "homework_submissions_matched_task_id_fkey",
        "homework_submissions",
        type_="foreignkey",
    )
    op.drop_table("structure_nodes_editable")
    op.drop_table("structure_nodes")
    op.drop_table("structure_snapshots")
    op.drop_table("reconciliation_previews")


def downgrade() -> None:
    """Rollback is intentionally not implemented (one-way migration)."""
    msg = (
        "drop is one-way; Phase 3 will introduce node_summary_raw and "
        "node_summary_final greenfield tables per KD6/KD10/KD11."
    )
    raise NotImplementedError(msg)
