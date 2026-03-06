"""FK rename to {tablename}_id convention + COMMENT ON tables/columns.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-05 12:00:00.000000+00:00
"""

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── FK Column Renames ──

    # material_nodes: parent_id → parent_materialnode_id
    op.alter_column(
        "material_nodes", "parent_id", new_column_name="parent_materialnode_id"
    )

    # material_entries: node_id → materialnode_id
    op.alter_column("material_entries", "node_id", new_column_name="materialnode_id")

    # material_entries: pending_job_id → job_id
    op.alter_column("material_entries", "pending_job_id", new_column_name="job_id")

    # slide_video_mappings: node_id → materialnode_id
    op.alter_column(
        "slide_video_mappings", "node_id", new_column_name="materialnode_id"
    )

    # slide_video_mappings: presentation_entry_id → presentation_materialentry_id
    op.alter_column(
        "slide_video_mappings",
        "presentation_entry_id",
        new_column_name="presentation_materialentry_id",
    )

    # slide_video_mappings: video_entry_id → video_materialentry_id
    op.alter_column(
        "slide_video_mappings",
        "video_entry_id",
        new_column_name="video_materialentry_id",
    )

    # structure_snapshots: node_id → materialnode_id
    op.alter_column("structure_snapshots", "node_id", new_column_name="materialnode_id")

    # jobs: node_id → materialnode_id
    op.alter_column("jobs", "node_id", new_column_name="materialnode_id")

    # ── Index renames (drop old, create new with correct column names) ──

    # material_nodes: parent_id index
    op.drop_index("ix_material_nodes_parent_id", table_name="material_nodes")
    op.create_index(
        "ix_material_nodes_parent_materialnode_id",
        "material_nodes",
        ["parent_materialnode_id"],
    )

    # material_entries: node_id index
    op.drop_index("ix_material_entries_node_id", table_name="material_entries")
    op.create_index(
        "ix_material_entries_materialnode_id",
        "material_entries",
        ["materialnode_id"],
    )

    # material_entries: pending_job_id index
    op.drop_index("ix_material_entries_pending_job_id", table_name="material_entries")
    op.create_index("ix_material_entries_job_id", "material_entries", ["job_id"])

    # slide_video_mappings: node_id index
    op.drop_index("ix_svm_node", table_name="slide_video_mappings")
    op.create_index(
        "ix_svm_materialnode",
        "slide_video_mappings",
        ["materialnode_id"],
    )

    # slide_video_mappings: presentation_entry_id index
    op.drop_index("ix_svm_presentation", table_name="slide_video_mappings")
    op.create_index(
        "ix_svm_pres_materialentry",
        "slide_video_mappings",
        ["presentation_materialentry_id"],
    )

    # slide_video_mappings: video_entry_id index
    op.drop_index("ix_svm_video", table_name="slide_video_mappings")
    op.create_index(
        "ix_svm_video_materialentry",
        "slide_video_mappings",
        ["video_materialentry_id"],
    )

    # structure_snapshots: node_id index
    op.drop_index("ix_snapshots_node", table_name="structure_snapshots")
    op.create_index(
        "ix_snapshots_materialnode",
        "structure_snapshots",
        ["materialnode_id"],
    )

    # structure_snapshots: unique identity index (uses COALESCE)
    op.drop_index("uq_snapshots_identity", table_name="structure_snapshots")
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshots_identity ON structure_snapshots ("
        "COALESCE(materialnode_id, "
        "'00000000-0000-0000-0000-000000000000'::uuid), "
        "node_fingerprint, mode)"
    )

    # jobs: node_id index
    op.drop_index("ix_jobs_node_id", table_name="jobs")
    op.create_index("ix_jobs_materialnode_id", "jobs", ["materialnode_id"])

    # ── COMMENT ON TABLE ──

    op.execute("COMMENT ON TABLE tenants IS 'Multi-tenant organizations'")
    op.execute(
        "COMMENT ON TABLE api_keys IS "
        "'Authentication keys with scope-based access control'"
    )
    op.execute(
        "COMMENT ON TABLE material_nodes IS "
        "'Hierarchical tree of course materials. "
        "Root node (parent IS NULL) = course'"
    )
    op.execute(
        "COMMENT ON TABLE material_entries IS "
        "'Individual learning materials (video, presentation, text, web)'"
    )
    op.execute(
        "COMMENT ON TABLE slide_video_mappings IS "
        "'Presentation slide to video timecode mappings'"
    )
    op.execute(
        "COMMENT ON TABLE structure_snapshots IS "
        "'LLM-generated course structure versions'"
    )
    op.execute(
        "COMMENT ON TABLE structure_nodes IS "
        "'Recursive tree of generated course structure elements'"
    )
    op.execute(
        "COMMENT ON TABLE jobs IS "
        "'Background task queue entries (ingestion, generation)'"
    )
    op.execute(
        "COMMENT ON TABLE external_service_calls IS "
        "'Audit log of all external API calls "
        "(LLM, transcription, etc.)'"
    )

    # ── COMMENT ON COLUMN (non-obvious fields) ──

    # api_keys
    op.execute(
        "COMMENT ON COLUMN api_keys.key_hash IS "
        "'SHA-256 hash of the API key. Raw key is never stored'"
    )
    op.execute(
        "COMMENT ON COLUMN api_keys.key_prefix IS "
        "'First 8 chars of the key for identification in logs'"
    )
    op.execute(
        "COMMENT ON COLUMN api_keys.scopes IS "
        "'JSON array of granted scopes (prep, check)'"
    )

    # material_nodes
    op.execute(
        "COMMENT ON COLUMN material_nodes.parent_materialnode_id IS "
        "'Self-referential FK. NULL = root node (course level)'"
    )
    op.execute(
        "COMMENT ON COLUMN material_nodes.node_fingerprint IS "
        "'Merkle hash of content subtree. "
        "NULL = stale, needs recompute'"
    )
    op.execute(
        "COMMENT ON COLUMN material_nodes.learning_goal IS "
        "'Pedagogical goal text for guided generation mode'"
    )

    # material_entries
    op.execute(
        "COMMENT ON COLUMN material_entries.materialnode_id IS "
        "'FK to parent MaterialNode in the tree'"
    )
    op.execute(
        "COMMENT ON COLUMN material_entries.job_id IS "
        "'FK to in-flight ingestion Job. NULL when idle'"
    )
    op.execute(
        "COMMENT ON COLUMN material_entries.processed_hash IS "
        "'SHA-256 of processed_content for Merkle tree'"
    )
    op.execute(
        "COMMENT ON COLUMN material_entries.raw_hash IS "
        "'SHA-256 of uploaded raw file for integrity detection'"
    )

    # slide_video_mappings
    op.execute(
        "COMMENT ON COLUMN slide_video_mappings.materialnode_id IS "
        "'FK to owning MaterialNode'"
    )
    op.execute(
        "COMMENT ON COLUMN slide_video_mappings.validation_state IS "
        "'Enum: pending_validation, validated, validation_failed'"
    )
    op.execute(
        "COMMENT ON COLUMN slide_video_mappings.blocking_factors IS "
        "'JSONB array of reasons preventing validation'"
    )

    # structure_snapshots
    op.execute(
        "COMMENT ON COLUMN structure_snapshots.materialnode_id IS "
        "'FK to target MaterialNode (root = whole course)'"
    )
    op.execute(
        "COMMENT ON COLUMN structure_snapshots.node_fingerprint IS "
        "'Merkle hash at generation time for idempotency'"
    )
    op.execute(
        "COMMENT ON COLUMN structure_snapshots.structure IS "
        "'JSONB of generated course structure'"
    )

    # structure_nodes
    op.execute(
        "COMMENT ON COLUMN structure_nodes.parent_structurenode_id IS "
        "'Self-referential FK. NULL = top-level module'"
    )
    op.execute(
        "COMMENT ON COLUMN structure_nodes.node_type IS "
        "'Enum: module, lesson, concept, exercise'"
    )
    op.execute(
        "COMMENT ON COLUMN structure_nodes.key_concepts IS "
        "'JSONB array of concept strings'"
    )
    op.execute(
        "COMMENT ON COLUMN structure_nodes.timecodes IS "
        "'JSONB of video timecode references'"
    )

    # jobs
    op.execute(
        "COMMENT ON COLUMN jobs.materialnode_id IS "
        "'FK to target MaterialNode. NULL for orphaned jobs'"
    )
    op.execute(
        "COMMENT ON COLUMN jobs.depends_on IS "
        "'JSONB array of Job UUIDs that must complete first'"
    )
    op.execute(
        "COMMENT ON COLUMN jobs.input_params IS 'JSONB of task-specific parameters'"
    )

    # external_service_calls — no non-obvious columns to comment


def downgrade() -> None:
    # ── Remove comments ──
    for table in (
        "tenants",
        "api_keys",
        "material_nodes",
        "material_entries",
        "slide_video_mappings",
        "structure_snapshots",
        "structure_nodes",
        "jobs",
        "external_service_calls",
    ):
        op.execute(f"COMMENT ON TABLE {table} IS NULL")

    # ── Reverse index renames ──
    op.drop_index("ix_jobs_materialnode_id", table_name="jobs")
    op.create_index("ix_jobs_node_id", "jobs", ["materialnode_id"])

    op.drop_index("uq_snapshots_identity", table_name="structure_snapshots")
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshots_identity "
        "ON structure_snapshots ("
        "COALESCE(materialnode_id, "
        "'00000000-0000-0000-0000-000000000000'::uuid), "
        "node_fingerprint, mode)"
    )

    op.drop_index("ix_snapshots_materialnode", table_name="structure_snapshots")
    op.create_index(
        "ix_snapshots_node",
        "structure_snapshots",
        ["materialnode_id"],
    )

    op.drop_index("ix_svm_video_materialentry", table_name="slide_video_mappings")
    op.create_index(
        "ix_svm_video",
        "slide_video_mappings",
        ["video_materialentry_id"],
    )

    op.drop_index("ix_svm_pres_materialentry", table_name="slide_video_mappings")
    op.create_index(
        "ix_svm_presentation",
        "slide_video_mappings",
        ["presentation_materialentry_id"],
    )

    op.drop_index("ix_svm_materialnode", table_name="slide_video_mappings")
    op.create_index("ix_svm_node", "slide_video_mappings", ["materialnode_id"])

    op.drop_index("ix_material_entries_job_id", table_name="material_entries")
    op.create_index(
        "ix_material_entries_pending_job_id",
        "material_entries",
        ["job_id"],
    )

    op.drop_index(
        "ix_material_entries_materialnode_id",
        table_name="material_entries",
    )
    op.create_index(
        "ix_material_entries_node_id",
        "material_entries",
        ["materialnode_id"],
    )

    op.drop_index(
        "ix_material_nodes_parent_materialnode_id",
        table_name="material_nodes",
    )
    op.create_index(
        "ix_material_nodes_parent_id",
        "material_nodes",
        ["parent_materialnode_id"],
    )

    # ── Reverse FK column renames ──
    op.alter_column("jobs", "materialnode_id", new_column_name="node_id")
    op.alter_column("structure_snapshots", "materialnode_id", new_column_name="node_id")
    op.alter_column(
        "slide_video_mappings",
        "video_materialentry_id",
        new_column_name="video_entry_id",
    )
    op.alter_column(
        "slide_video_mappings",
        "presentation_materialentry_id",
        new_column_name="presentation_entry_id",
    )
    op.alter_column(
        "slide_video_mappings",
        "materialnode_id",
        new_column_name="node_id",
    )
    op.alter_column("material_entries", "job_id", new_column_name="pending_job_id")
    op.alter_column("material_entries", "materialnode_id", new_column_name="node_id")
    op.alter_column(
        "material_nodes",
        "parent_materialnode_id",
        new_column_name="parent_id",
    )
