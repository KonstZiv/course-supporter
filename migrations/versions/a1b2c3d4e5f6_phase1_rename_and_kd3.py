"""Phase 1: data model rename + KD3 prep + course_root_id denormalization

Revision ID: phase1_rename_and_kd3
Revises: f3a4b5c6d7e8
Create Date: 2026-05-01 00:00:00.000000

Phase 1 (vision §5 rows 1.1-1.4) — single consolidated DDL migration covering:

  - 1.1: ``material_nodes`` → ``course_nodes``; collapse ``node_fingerprint`` into
         existing ``content_hash`` (KD9); rename self-FK column
         ``parent_materialnode_id`` → ``parent_id`` (KD-eta.1).
  - 1.2: ``material_entries`` → ``authored_documents``; drop legacy
         ``processed_content`` + ``outline_content`` + ``processed_hash``; rename
         FK column ``materialnode_id`` → ``course_node_id``; add denormalized
         ``course_root_id`` (KD-delta).
  - 1.3: ``material_macro_sections`` → ``document_summaries``; drop position
         columns + ``order`` (POST-MERGE §2.2 amendment #1 per INVESTIGATION §1.4
         authoritative listing — 1:1 with AuthoredDocument makes ordering
         meaningless); resize ``title`` 500 → 128 (KD-theta.1); add ``description`` +
         concept jsonb columns (KD-gamma) + ``content_char_count`` + ``course_root_id``
         (KD-delta); ADD UNIQUE on ``authored_document_id`` (KD-theta.2 — 1:1 invariant);
         rename FK column ``material_entry_id`` → ``authored_document_id``.
  - 1.4: ``material_segments`` → ``document_segments``; rename FK column
         ``macro_section_id`` → ``document_summary_id``; add concept jsonb
         columns (KD-gamma) + ``content_char_count`` + ``course_root_id`` (KD-delta);
         add 2 GIN indexes with ``jsonb_path_ops`` opclass (KD-gamma).

KD-theta pre-flight gates run BEFORE any destructive op. Hard-fail with
``RuntimeError`` if invariant pre-conditions don't hold:

  - theta.1: title length > 128 on ``material_macro_sections``.
  - theta.2: 1:1 invariant duplicates on ``material_macro_sections.material_entry_id``.
  - KD-delta orphan check: ``material_entries.materialnode_id`` pointing at
    non-existent ``material_nodes`` rows (would block ``course_root_id``
    backfill recursive CTE).

NO auto-remediation. Triage SQL embedded in error messages. Alembic auto-rolls
back on raise; re-run safe from clean state.

Per C2 lock (pre-flight): DB enum type identifier
``material_macro_section_status_enum`` is preserved unchanged. ORM-level rename
in sub-area ``models`` keeps the same DB enum backing via SQLAlchemy
``Enum(..., name="material_macro_section_status_enum")``. Do NOT issue
``ALTER TYPE ... RENAME TO ...`` here.

Per C3 lock (pre-flight): all FK constraint name updates use
``ALTER TABLE <child> RENAME CONSTRAINT``. Postgres auto-updates ``confrelid``
when a parent table is renamed; no DROP+ADD needed for any constraint.

POST-MERGE-NOTES amendments per rule #16:

  1. PHASE.md §2.2 sub-area ``schema`` enumeration was abridged (listed DROP
     ``start_pos`` + ``end_pos`` only). INVESTIGATION.md §1.4 authoritatively
     listed ``order`` as a third DROP target on document_summaries — covered
     here.
  2. Composite index ``ix_material_macro_sections_entry_order`` on
     ``(material_entry_id, order)`` is dropped entirely (premise removed by
     ``order`` column drop); not replaced — UNIQUE on
     ``authored_document_id`` provides sufficient btree backing for FK
     lookups.
  3. Filename hex ``a1b2c3d4e5f6`` is filesystem-level identifier only;
     ``revision`` field set to slug ``phase1_rename_and_kd3`` per pre-flight
     C4(b) lock (deviation from Phase 0 where ``revision`` = filename hex).

Downgrade is best-effort. Dropped content columns
(``processed_content``, ``outline_content``, ``processed_hash``,
``start_pos``, ``end_pos``, ``order`` on document_summaries,
``node_fingerprint``) cannot be restored — original data is unrecoverable.
Reverse migration restores columns as NULLABLE without backfill (structural
recovery only). Production downgrade unsupported.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "phase1_rename_and_kd3"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply Phase 1 schema changes."""
    conn = op.get_bind()

    # =========================================================================
    # KD-theta + KD-delta pre-flight gates — run BEFORE any destructive operation.
    # Use pre-rename table/column names; renames have not happened yet.
    # =========================================================================

    # KD-theta.1: title length check on material_macro_sections (active rows only).
    title_violators = conn.execute(
        sa.text(
            "SELECT count(*) FROM material_macro_sections "
            "WHERE deleted_at IS NULL AND length(title) > 128"
        )
    ).scalar()
    if title_violators and title_violators > 0:
        # noqa block: error message body containing example SQL, not SQL execution.
        msg = (
            f"Phase 1.3 migration cannot proceed: {title_violators} active rows in "  # noqa: S608
            "material_macro_sections have title length > 128 chars. "
            "Resolve before re-running migration. Triage query:\n"
            "  SELECT id, length(title) AS len, substring(title, 1, 80) AS preview\n"
            "    FROM material_macro_sections\n"
            "   WHERE deleted_at IS NULL AND length(title) > 128\n"
            "   ORDER BY len DESC;"
        )
        raise RuntimeError(msg)

    # KD-theta.2: 1:1 invariant check (per material_entry_id, active rows only).
    duplicates = conn.execute(
        sa.text(
            "SELECT material_entry_id, count(*) AS dup_count "
            "  FROM material_macro_sections "
            " WHERE deleted_at IS NULL "
            " GROUP BY material_entry_id "
            "HAVING count(*) > 1 "
            "ORDER BY count(*) DESC LIMIT 10"
        )
    ).fetchall()
    if duplicates:
        details = "\n".join(
            f"  - material_entry_id={row[0]}: {row[1]} summaries" for row in duplicates
        )
        raise RuntimeError(
            f"Phase 1.3 migration cannot proceed: {len(duplicates)} authored_documents "
            "have multiple active document_summaries (1:1 invariant violation). "
            f"Top 10 violators:\n{details}\n\n"
            "Resolve manually before re-running. Likely cause: pre-Phase-1 "
            "pipeline produced duplicate macro_sections (data anomaly). "
            "Remediation: keep most recent per entry, hard-delete others, OR "
            "investigate data corruption source first."
        )

    # KD-delta orphan check: material_entries.materialnode_id → material_nodes(id).
    # Required pre-condition for course_root_id backfill via recursive CTE.
    orphans = conn.execute(
        sa.text(
            "SELECT count(*) FROM material_entries me "
            " LEFT JOIN material_nodes mn ON mn.id = me.materialnode_id "
            "WHERE mn.id IS NULL"
        )
    ).scalar()
    if orphans and orphans > 0:
        msg = (
            f"Phase 1.2 migration cannot proceed: {orphans} material_entries rows "  # noqa: S608
            "reference non-existent material_nodes (KD-delta orphan check failed). "
            "This would break course_root_id backfill via recursive CTE. "
            "Triage query:\n"
            "  SELECT id, materialnode_id, source_url\n"
            "    FROM material_entries me\n"
            "   WHERE NOT EXISTS (\n"
            "             SELECT 1 FROM material_nodes mn\n"
            "              WHERE mn.id = me.materialnode_id)\n"
            "   LIMIT 20;\n\n"
            "Resolve manually: hard-delete orphan rows OR investigate "
            "corruption source first. Do not auto-fix in migration."
        )
        raise RuntimeError(msg)

    # =========================================================================
    # Phase 1.1 — material_nodes → course_nodes
    # =========================================================================

    # Backfill content_hash from node_fingerprint where missing (KD9 collapse).
    # The race-protection trigger (block_update_on_soft_deleted) raises on any
    # non-deleted_at UPDATE of soft-deleted rows. Schema-migration data backfill
    # is the legitimate bypass case (DDL-privileged context); the trigger
    # exists to guard application-level writes, not migrations. Disable per
    # table around each backfill UPDATE, re-enable immediately after.
    op.execute(
        "ALTER TABLE material_nodes "
        "DISABLE TRIGGER trg_material_nodes_block_update_when_deleted"
    )
    op.execute(
        "UPDATE material_nodes "
        "   SET content_hash = node_fingerprint "
        " WHERE content_hash IS NULL AND node_fingerprint IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE material_nodes "
        "ENABLE TRIGGER trg_material_nodes_block_update_when_deleted"
    )

    # Drop legacy node_fingerprint column.
    op.drop_column("material_nodes", "node_fingerprint")

    # Rename self-FK column parent_materialnode_id → parent_id (KD-eta.1).
    op.alter_column(
        "material_nodes",
        "parent_materialnode_id",
        new_column_name="parent_id",
    )

    # Rename table material_nodes → course_nodes.
    op.rename_table("material_nodes", "course_nodes")

    # Rename indexes (Postgres preserves index defs across table rename but the
    # index NAMES retain the old prefix — explicit rename for cleanliness).
    op.execute("ALTER INDEX ix_material_nodes_active RENAME TO ix_course_nodes_active")
    op.execute(
        "ALTER INDEX ix_material_nodes_parent_materialnode_id "
        "RENAME TO ix_course_nodes_parent_id"
    )
    op.execute(
        "ALTER INDEX ix_material_nodes_tenant_id RENAME TO ix_course_nodes_tenant_id"
    )
    op.execute("ALTER INDEX material_nodes_pkey RENAME TO course_nodes_pkey")

    # Rename constraints (RENAME CONSTRAINT only per C3 lock — Postgres
    # auto-updates confrelid when parent renames).
    op.execute(
        "ALTER TABLE course_nodes RENAME CONSTRAINT "
        "material_nodes_parent_materialnode_id_fkey TO course_nodes_parent_id_fkey"
    )
    op.execute(
        "ALTER TABLE course_nodes RENAME CONSTRAINT "
        "material_nodes_tenant_id_fkey TO course_nodes_tenant_id_fkey"
    )

    # Rename trigger (Postgres does NOT auto-rename triggers on table rename;
    # event_object_table updates but trigger NAME is sticky — explicit rename
    # required to maintain trg_<new_table>_block_update_when_deleted convention).
    op.execute(
        "ALTER TRIGGER trg_material_nodes_block_update_when_deleted "
        "ON course_nodes RENAME TO trg_course_nodes_block_update_when_deleted"
    )

    # =========================================================================
    # Phase 1.2 — material_entries → authored_documents
    # =========================================================================

    # Drop legacy content columns per vision §1.2.
    op.drop_column("material_entries", "processed_content")
    op.drop_column("material_entries", "outline_content")
    op.drop_column("material_entries", "processed_hash")

    # Rename FK column materialnode_id → course_node_id (matches renamed parent
    # table; aligns with existing jobs.course_node_id precedent from 0.3).
    op.alter_column(
        "material_entries",
        "materialnode_id",
        new_column_name="course_node_id",
    )

    # Add course_root_id column (nullable; backfill below; SET NOT NULL after).
    op.add_column(
        "material_entries",
        sa.Column(
            "course_root_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    # Backfill course_root_id via recursive CTE walking parent chain to root.
    # Uses post-Phase-1.1 names: course_nodes.parent_id (just renamed above).
    # Trigger disabled around UPDATE — see Phase 1.1 backfill comment for rationale.
    op.execute(
        "ALTER TABLE material_entries "
        "DISABLE TRIGGER trg_material_entries_block_update_when_deleted"
    )
    op.execute(
        """
        WITH RECURSIVE roots AS (
            SELECT id, id AS root_id
              FROM course_nodes
             WHERE parent_id IS NULL
            UNION ALL
            SELECT cn.id, r.root_id
              FROM course_nodes cn
              JOIN roots r ON cn.parent_id = r.id
        )
        UPDATE material_entries me
           SET course_root_id = roots.root_id
          FROM roots
         WHERE me.course_node_id = roots.id
        """
    )
    op.execute(
        "ALTER TABLE material_entries "
        "ENABLE TRIGGER trg_material_entries_block_update_when_deleted"
    )

    # Defensive sanity check: KD-delta orphan pre-flight covered material_entries
    # → material_nodes orphans, but pathological prior states (manual DB
    # intervention, partial backup restore) may have created course_nodes
    # self-FK orphans. Recursive CTE silently skips unreachable nodes; downstream
    # material_entries with course_node_id pointing at the orphan get NULL after
    # backfill. Detect with custom RuntimeError + triage SQL before SET NOT NULL
    # surfaces the same condition as opaque "column contains null values".
    nulls = conn.execute(
        sa.text("SELECT count(*) FROM material_entries WHERE course_root_id IS NULL")
    ).scalar()
    if nulls and nulls > 0:
        msg = (
            f"Phase 1.2 course_root_id backfill left {nulls} rows with NULL. "  # noqa: S608
            "Likely cause: parent chain includes course_nodes self-FK orphan or "
            "unreachable parent_id (recursive CTE skips unreachable nodes). "
            "Triage:\n"
            "  SELECT id, course_node_id FROM material_entries\n"
            "   WHERE course_root_id IS NULL LIMIT 20;\n"
            "  SELECT id, parent_id FROM course_nodes\n"
            "   WHERE parent_id IS NOT NULL\n"
            "     AND NOT EXISTS (SELECT 1 FROM course_nodes p WHERE p.id = course_nodes.parent_id)\n"
            "   LIMIT 20;\n"
            "Resolve manually: hard-delete orphan course_nodes OR investigate "
            "data integrity source first."
        )
        raise RuntimeError(msg)

    # Set NOT NULL (orphan pre-check + post-backfill null check above guarantee
    # no NULLs).
    op.alter_column(
        "material_entries",
        "course_root_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # Rename table material_entries → authored_documents.
    op.rename_table("material_entries", "authored_documents")

    # Rename indexes.
    op.execute(
        "ALTER INDEX ix_material_entries_active RENAME TO ix_authored_documents_active"
    )
    op.execute(
        "ALTER INDEX ix_material_entries_job_id RENAME TO ix_authored_documents_job_id"
    )
    op.execute(
        "ALTER INDEX ix_material_entries_materialnode_id "
        "RENAME TO ix_authored_documents_course_node_id"
    )
    op.execute(
        "ALTER INDEX ix_material_entries_raw_hash "
        "RENAME TO ix_authored_documents_raw_hash"
    )
    op.execute("ALTER INDEX material_entries_pkey RENAME TO authored_documents_pkey")

    # Rename constraints.
    op.execute(
        "ALTER TABLE authored_documents RENAME CONSTRAINT "
        "material_entries_materialnode_id_fkey "
        "TO authored_documents_course_node_id_fkey"
    )
    op.execute(
        "ALTER TABLE authored_documents RENAME CONSTRAINT "
        "material_entries_job_id_fkey TO authored_documents_job_id_fkey"
    )

    # Add KD-delta FK + B-tree index for course_root_id.
    op.create_foreign_key(
        "authored_documents_course_root_id_fkey",
        "authored_documents",
        "course_nodes",
        ["course_root_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_authored_documents_course_root_id",
        "authored_documents",
        ["course_root_id"],
    )

    # Rename trigger.
    op.execute(
        "ALTER TRIGGER trg_material_entries_block_update_when_deleted "
        "ON authored_documents "
        "RENAME TO trg_authored_documents_block_update_when_deleted"
    )

    # =========================================================================
    # Phase 1.3 — material_macro_sections → document_summaries
    # =========================================================================

    # Drop CHECK constraints tied to position columns being dropped.
    op.drop_constraint(
        "ck_macro_start_pos_nonneg", "material_macro_sections", type_="check"
    )
    op.drop_constraint(
        "ck_macro_end_pos_gt_start", "material_macro_sections", type_="check"
    )

    # Drop composite index (premise removed — `order` column being dropped;
    # POST-MERGE §2.2 amendment #2: not replaced — UNIQUE provides btree).
    op.drop_index(
        "ix_material_macro_sections_entry_order",
        table_name="material_macro_sections",
    )

    # Drop position columns + `order` (POST-MERGE §2.2 amendment #1 per
    # INVESTIGATION §1.4 — DocumentSummary is 1:1 with AuthoredDocument).
    op.drop_column("material_macro_sections", "start_pos")
    op.drop_column("material_macro_sections", "end_pos")
    op.drop_column("material_macro_sections", "order")

    # Resize title 500 → 128 (KD-theta.1 — pre-flight gate verified zero violators).
    op.alter_column(
        "material_macro_sections",
        "title",
        type_=sa.String(128),
        existing_type=sa.String(500),
        existing_nullable=False,
    )

    # Add new columns per vision §1.3 + KD-gamma + KD-delta.
    op.add_column(
        "material_macro_sections",
        sa.Column("description", sa.String(512), nullable=True),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column(
            "main_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column(
            "secondary_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column("content_char_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column(
            "course_root_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    # Rename FK column material_entry_id → authored_document_id.
    op.alter_column(
        "material_macro_sections",
        "material_entry_id",
        new_column_name="authored_document_id",
    )

    # Backfill course_root_id by inheriting from authored_documents (Phase 1.2
    # already populated authored_documents.course_root_id above). Trigger
    # disabled around UPDATE — see Phase 1.1 backfill comment for rationale.
    op.execute(
        "ALTER TABLE material_macro_sections "
        "DISABLE TRIGGER trg_material_macro_sections_block_update_when_deleted"
    )
    op.execute(
        """
        UPDATE material_macro_sections ds
           SET course_root_id = ad.course_root_id
          FROM authored_documents ad
         WHERE ds.authored_document_id = ad.id
        """
    )
    op.execute(
        "ALTER TABLE material_macro_sections "
        "ENABLE TRIGGER trg_material_macro_sections_block_update_when_deleted"
    )

    # Defensive sanity check: catch authored_document_id pointing at non-existent
    # authored_documents row (would leave course_root_id NULL post-backfill).
    nulls = conn.execute(
        sa.text(
            "SELECT count(*) FROM material_macro_sections WHERE course_root_id IS NULL"
        )
    ).scalar()
    if nulls and nulls > 0:
        msg = (
            f"Phase 1.3 course_root_id backfill left {nulls} rows with NULL. "  # noqa: S608
            "Likely cause: material_macro_sections.authored_document_id points "
            "at non-existent authored_documents row, OR upstream Phase 1.2 left "
            "authored_documents.course_root_id NULL (should not happen — Phase 1.2 "
            "null-check above would have raised first). Triage:\n"
            "  SELECT id, authored_document_id FROM material_macro_sections\n"
            "   WHERE course_root_id IS NULL LIMIT 20;\n"
            "Resolve manually: investigate parent FK integrity first."
        )
        raise RuntimeError(msg)

    # Set NOT NULL.
    op.alter_column(
        "material_macro_sections",
        "course_root_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # ADD UNIQUE on authored_document_id (KD-theta.2 — 1:1 invariant lock).
    op.create_unique_constraint(
        "uq_document_summaries_authored_document_id",
        "material_macro_sections",
        ["authored_document_id"],
    )

    # Rename table material_macro_sections → document_summaries.
    op.rename_table("material_macro_sections", "document_summaries")

    # Rename remaining indexes.
    op.execute(
        "ALTER INDEX ix_material_macro_sections_active "
        "RENAME TO ix_document_summaries_active"
    )
    op.execute(
        "ALTER INDEX ix_material_macro_sections_material_entry_id "
        "RENAME TO ix_document_summaries_authored_document_id"
    )
    op.execute(
        "ALTER INDEX ix_material_macro_sections_unready "
        "RENAME TO ix_document_summaries_unready"
    )
    op.execute(
        "ALTER INDEX material_macro_sections_pkey RENAME TO document_summaries_pkey"
    )

    # Rename constraints.
    op.execute(
        "ALTER TABLE document_summaries RENAME CONSTRAINT "
        "material_macro_sections_material_entry_id_fkey "
        "TO document_summaries_authored_document_id_fkey"
    )
    op.execute(
        "ALTER TABLE document_summaries RENAME CONSTRAINT "
        "material_macro_sections_llm_call_id_fkey "
        "TO document_summaries_llm_call_id_fkey"
    )

    # Add KD-delta FK + B-tree index for course_root_id.
    op.create_foreign_key(
        "document_summaries_course_root_id_fkey",
        "document_summaries",
        "course_nodes",
        ["course_root_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_document_summaries_course_root_id",
        "document_summaries",
        ["course_root_id"],
    )

    # Rename trigger.
    op.execute(
        "ALTER TRIGGER trg_material_macro_sections_block_update_when_deleted "
        "ON document_summaries "
        "RENAME TO trg_document_summaries_block_update_when_deleted"
    )

    # C2 lock: DB enum type identifier `material_macro_section_status_enum`
    # preserved unchanged; ORM-level rename in sub-area `models` keeps the same
    # DB enum backing. Do NOT issue ALTER TYPE ... RENAME TO ... here.

    # =========================================================================
    # Phase 1.4 — material_segments → document_segments
    # =========================================================================

    # Add new columns per vision §1.4 + KD-gamma + KD-delta.
    op.add_column(
        "material_segments",
        sa.Column(
            "main_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "material_segments",
        sa.Column(
            "secondary_concepts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "material_segments",
        sa.Column("content_char_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_segments",
        sa.Column(
            "course_root_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    # Rename FK column macro_section_id → document_summary_id (Phase 1.3 already
    # renamed the parent table; Postgres auto-updates confrelid).
    op.alter_column(
        "material_segments",
        "macro_section_id",
        new_column_name="document_summary_id",
    )

    # Backfill course_root_id by inheriting from document_summaries. Trigger
    # disabled around UPDATE — see Phase 1.1 backfill comment for rationale.
    op.execute(
        "ALTER TABLE material_segments "
        "DISABLE TRIGGER trg_material_segments_block_update_when_deleted"
    )
    op.execute(
        """
        UPDATE material_segments seg
           SET course_root_id = sm.course_root_id
          FROM document_summaries sm
         WHERE seg.document_summary_id = sm.id
        """
    )
    op.execute(
        "ALTER TABLE material_segments "
        "ENABLE TRIGGER trg_material_segments_block_update_when_deleted"
    )

    # Defensive sanity check: catch document_summary_id pointing at non-existent
    # document_summaries row (would leave course_root_id NULL post-backfill).
    nulls = conn.execute(
        sa.text("SELECT count(*) FROM material_segments WHERE course_root_id IS NULL")
    ).scalar()
    if nulls and nulls > 0:
        msg = (
            f"Phase 1.4 course_root_id backfill left {nulls} rows with NULL. "  # noqa: S608
            "Likely cause: material_segments.document_summary_id points at "
            "non-existent document_summaries row, OR upstream Phase 1.3 left "
            "document_summaries.course_root_id NULL (should not happen — Phase 1.3 "
            "null-check above would have raised first). Triage:\n"
            "  SELECT id, document_summary_id FROM material_segments\n"
            "   WHERE course_root_id IS NULL LIMIT 20;\n"
            "Resolve manually: investigate parent FK integrity first."
        )
        raise RuntimeError(msg)

    # Set NOT NULL.
    op.alter_column(
        "material_segments",
        "course_root_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # Rename table material_segments → document_segments.
    op.rename_table("material_segments", "document_segments")

    # Rename existing indexes.
    op.execute(
        "ALTER INDEX ix_material_segments_active RENAME TO ix_document_segments_active"
    )
    op.execute(
        "ALTER INDEX ix_material_segments_macro_order "
        "RENAME TO ix_document_segments_summary_order"
    )
    op.execute(
        "ALTER INDEX ix_material_segments_macro_section_id "
        "RENAME TO ix_document_segments_document_summary_id"
    )
    op.execute(
        "ALTER INDEX ix_material_segments_macro_start_pos "
        "RENAME TO ix_document_segments_summary_start_pos"
    )
    op.execute("ALTER INDEX material_segments_pkey RENAME TO document_segments_pkey")

    # Rename constraints.
    op.execute(
        "ALTER TABLE document_segments RENAME CONSTRAINT "
        "material_segments_macro_section_id_fkey "
        "TO document_segments_document_summary_id_fkey"
    )
    op.execute(
        "ALTER TABLE document_segments RENAME CONSTRAINT "
        "material_segments_llm_call_id_fkey "
        "TO document_segments_llm_call_id_fkey"
    )

    # Add KD-delta FK + B-tree index for course_root_id.
    op.create_foreign_key(
        "document_segments_course_root_id_fkey",
        "document_segments",
        "course_nodes",
        ["course_root_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_document_segments_course_root_id",
        "document_segments",
        ["course_root_id"],
    )

    # KD-gamma: GIN indexes with jsonb_path_ops opclass — DocumentSegment ONLY.
    # DocumentSummary concept columns receive NO GIN per vision §2.2 line 274
    # (single source of truth for concept search is DocumentSegment).
    op.execute(
        "CREATE INDEX ix_document_segments_main_concepts_gin "
        "ON document_segments USING gin (main_concepts jsonb_path_ops)"
    )
    op.execute(
        "CREATE INDEX ix_document_segments_secondary_concepts_gin "
        "ON document_segments USING gin (secondary_concepts jsonb_path_ops)"
    )

    # Rename trigger.
    op.execute(
        "ALTER TRIGGER trg_material_segments_block_update_when_deleted "
        "ON document_segments "
        "RENAME TO trg_document_segments_block_update_when_deleted"
    )


def downgrade() -> None:
    """Best-effort reverse migration. NOT supported in production.

    Dropped content columns (``processed_content``, ``outline_content``,
    ``processed_hash``, ``start_pos``, ``end_pos``, ``order`` on
    document_summaries, ``node_fingerprint``) are restored as NULLABLE without
    backfill — original data is unrecoverable. Ops are reversed in strict LIFO
    order: 1.4 → 1.3 → 1.2 → 1.1.
    """

    # ---------- Phase 1.4 reverse ----------
    op.execute(
        "ALTER TRIGGER trg_document_segments_block_update_when_deleted "
        "ON document_segments "
        "RENAME TO trg_material_segments_block_update_when_deleted"
    )
    op.execute("DROP INDEX IF EXISTS ix_document_segments_secondary_concepts_gin")
    op.execute("DROP INDEX IF EXISTS ix_document_segments_main_concepts_gin")
    op.drop_index("ix_document_segments_course_root_id", table_name="document_segments")
    op.drop_constraint(
        "document_segments_course_root_id_fkey",
        "document_segments",
        type_="foreignkey",
    )
    op.execute(
        "ALTER TABLE document_segments RENAME CONSTRAINT "
        "document_segments_llm_call_id_fkey TO material_segments_llm_call_id_fkey"
    )
    op.execute(
        "ALTER TABLE document_segments RENAME CONSTRAINT "
        "document_segments_document_summary_id_fkey "
        "TO material_segments_macro_section_id_fkey"
    )
    op.execute("ALTER INDEX document_segments_pkey RENAME TO material_segments_pkey")
    op.execute(
        "ALTER INDEX ix_document_segments_summary_start_pos "
        "RENAME TO ix_material_segments_macro_start_pos"
    )
    op.execute(
        "ALTER INDEX ix_document_segments_document_summary_id "
        "RENAME TO ix_material_segments_macro_section_id"
    )
    op.execute(
        "ALTER INDEX ix_document_segments_summary_order "
        "RENAME TO ix_material_segments_macro_order"
    )
    op.execute(
        "ALTER INDEX ix_document_segments_active RENAME TO ix_material_segments_active"
    )
    op.rename_table("document_segments", "material_segments")
    op.alter_column(
        "material_segments",
        "document_summary_id",
        new_column_name="macro_section_id",
    )
    op.drop_column("material_segments", "course_root_id")
    op.drop_column("material_segments", "content_char_count")
    op.drop_column("material_segments", "secondary_concepts")
    op.drop_column("material_segments", "main_concepts")

    # ---------- Phase 1.3 reverse ----------
    op.execute(
        "ALTER TRIGGER trg_document_summaries_block_update_when_deleted "
        "ON document_summaries "
        "RENAME TO trg_material_macro_sections_block_update_when_deleted"
    )
    op.drop_index(
        "ix_document_summaries_course_root_id", table_name="document_summaries"
    )
    op.drop_constraint(
        "document_summaries_course_root_id_fkey",
        "document_summaries",
        type_="foreignkey",
    )
    op.execute(
        "ALTER TABLE document_summaries RENAME CONSTRAINT "
        "document_summaries_llm_call_id_fkey "
        "TO material_macro_sections_llm_call_id_fkey"
    )
    op.execute(
        "ALTER TABLE document_summaries RENAME CONSTRAINT "
        "document_summaries_authored_document_id_fkey "
        "TO material_macro_sections_material_entry_id_fkey"
    )
    op.execute(
        "ALTER INDEX document_summaries_pkey RENAME TO material_macro_sections_pkey"
    )
    op.execute(
        "ALTER INDEX ix_document_summaries_unready "
        "RENAME TO ix_material_macro_sections_unready"
    )
    op.execute(
        "ALTER INDEX ix_document_summaries_authored_document_id "
        "RENAME TO ix_material_macro_sections_material_entry_id"
    )
    op.execute(
        "ALTER INDEX ix_document_summaries_active "
        "RENAME TO ix_material_macro_sections_active"
    )
    op.rename_table("document_summaries", "material_macro_sections")
    op.drop_constraint(
        "uq_document_summaries_authored_document_id",
        "material_macro_sections",
        type_="unique",
    )
    op.alter_column(
        "material_macro_sections",
        "authored_document_id",
        new_column_name="material_entry_id",
    )
    op.drop_column("material_macro_sections", "course_root_id")
    op.drop_column("material_macro_sections", "content_char_count")
    op.drop_column("material_macro_sections", "secondary_concepts")
    op.drop_column("material_macro_sections", "main_concepts")
    op.drop_column("material_macro_sections", "description")
    op.alter_column(
        "material_macro_sections",
        "title",
        type_=sa.String(500),
        existing_type=sa.String(128),
        existing_nullable=False,
    )
    # Restore dropped columns as NULLABLE — data unrecoverable.
    op.add_column(
        "material_macro_sections",
        sa.Column("order", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column("end_pos", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_macro_sections",
        sa.Column("start_pos", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_material_macro_sections_entry_order",
        "material_macro_sections",
        ["material_entry_id", "order"],
    )
    op.create_check_constraint(
        "ck_macro_end_pos_gt_start",
        "material_macro_sections",
        "end_pos > start_pos",
    )
    op.create_check_constraint(
        "ck_macro_start_pos_nonneg",
        "material_macro_sections",
        "start_pos >= 0",
    )

    # ---------- Phase 1.2 reverse ----------
    op.execute(
        "ALTER TRIGGER trg_authored_documents_block_update_when_deleted "
        "ON authored_documents "
        "RENAME TO trg_material_entries_block_update_when_deleted"
    )
    op.drop_index(
        "ix_authored_documents_course_root_id", table_name="authored_documents"
    )
    op.drop_constraint(
        "authored_documents_course_root_id_fkey",
        "authored_documents",
        type_="foreignkey",
    )
    op.execute(
        "ALTER TABLE authored_documents RENAME CONSTRAINT "
        "authored_documents_job_id_fkey TO material_entries_job_id_fkey"
    )
    op.execute(
        "ALTER TABLE authored_documents RENAME CONSTRAINT "
        "authored_documents_course_node_id_fkey "
        "TO material_entries_materialnode_id_fkey"
    )
    op.execute("ALTER INDEX authored_documents_pkey RENAME TO material_entries_pkey")
    op.execute(
        "ALTER INDEX ix_authored_documents_raw_hash "
        "RENAME TO ix_material_entries_raw_hash"
    )
    op.execute(
        "ALTER INDEX ix_authored_documents_course_node_id "
        "RENAME TO ix_material_entries_materialnode_id"
    )
    op.execute(
        "ALTER INDEX ix_authored_documents_job_id RENAME TO ix_material_entries_job_id"
    )
    op.execute(
        "ALTER INDEX ix_authored_documents_active RENAME TO ix_material_entries_active"
    )
    op.rename_table("authored_documents", "material_entries")
    op.drop_column("material_entries", "course_root_id")
    op.alter_column(
        "material_entries",
        "course_node_id",
        new_column_name="materialnode_id",
    )
    # Restore dropped columns as NULLABLE.
    op.add_column(
        "material_entries",
        sa.Column("processed_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "material_entries",
        sa.Column(
            "outline_content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "material_entries",
        sa.Column("processed_content", sa.Text(), nullable=True),
    )

    # ---------- Phase 1.1 reverse ----------
    op.execute(
        "ALTER TRIGGER trg_course_nodes_block_update_when_deleted "
        "ON course_nodes "
        "RENAME TO trg_material_nodes_block_update_when_deleted"
    )
    op.execute(
        "ALTER TABLE course_nodes RENAME CONSTRAINT "
        "course_nodes_tenant_id_fkey TO material_nodes_tenant_id_fkey"
    )
    op.execute(
        "ALTER TABLE course_nodes RENAME CONSTRAINT "
        "course_nodes_parent_id_fkey "
        "TO material_nodes_parent_materialnode_id_fkey"
    )
    op.execute("ALTER INDEX course_nodes_pkey RENAME TO material_nodes_pkey")
    op.execute(
        "ALTER INDEX ix_course_nodes_tenant_id RENAME TO ix_material_nodes_tenant_id"
    )
    op.execute(
        "ALTER INDEX ix_course_nodes_parent_id "
        "RENAME TO ix_material_nodes_parent_materialnode_id"
    )
    op.execute("ALTER INDEX ix_course_nodes_active RENAME TO ix_material_nodes_active")
    op.rename_table("course_nodes", "material_nodes")
    op.alter_column(
        "material_nodes",
        "parent_id",
        new_column_name="parent_materialnode_id",
    )
    # Restore node_fingerprint as NULLABLE (no backfill — content_hash retained
    # the values; reverse-direction copy is structural-only).
    op.add_column(
        "material_nodes",
        sa.Column("node_fingerprint", sa.String(64), nullable=True),
    )
