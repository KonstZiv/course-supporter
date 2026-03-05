"""Drop Course table and all legacy tables (S3-013).

Remove: courses, source_materials, modules, lessons, concepts, exercises.
Drop course_id columns from: material_nodes, jobs, structure_snapshots.
Add tenant_id to jobs (migrated from course → material_nodes → tenant).

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-03-05 18:00:00.000000+00:00
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    # ── 1. Jobs: add tenant_id, migrate data, drop course_id ──

    op.add_column("jobs", sa.Column("tenant_id", sa.Uuid(), nullable=True))

    # Migrate: job.tenant_id = material_node.tenant_id via job.node_id
    op.execute(
        """
        UPDATE jobs j
        SET tenant_id = mn.tenant_id
        FROM material_nodes mn
        WHERE j.node_id = mn.id
          AND j.tenant_id IS NULL
        """
    )

    # FK + index for tenant_id
    op.create_foreign_key(
        "fk_jobs_tenant",
        "jobs",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])

    # Drop course_id from jobs
    op.drop_index("ix_jobs_course_id", table_name="jobs")
    op.drop_constraint("jobs_course_id_fkey", "jobs", type_="foreignkey")
    op.drop_column("jobs", "course_id")

    # ── 2. Structure snapshots: drop course_id ──

    # Recreate unique index without course_id
    op.drop_index("uq_snapshots_identity", table_name="structure_snapshots")
    op.create_index(
        "uq_snapshots_identity",
        "structure_snapshots",
        [
            "node_id",
            "node_fingerprint",
            "mode",
        ],
        unique=True,
    )

    # Drop course_id column and its index/FK
    op.drop_index("ix_snapshots_course", table_name="structure_snapshots")
    op.execute(
        """
        ALTER TABLE structure_snapshots
        DROP CONSTRAINT IF EXISTS course_structure_snapshots_course_id_fkey
        """
    )
    op.drop_column("structure_snapshots", "course_id")

    # ── 3. Material nodes: drop course_id ──

    op.drop_index("ix_material_nodes_course_id", table_name="material_nodes")
    op.execute(
        """
        ALTER TABLE material_nodes
        DROP CONSTRAINT IF EXISTS material_nodes_course_id_fkey
        """
    )
    op.drop_column("material_nodes", "course_id")

    # ── 4. Drop legacy tables (leaf → parent order) ──

    op.drop_table("exercises")
    op.drop_table("concepts")
    op.drop_table("lessons")
    op.drop_table("source_materials")
    op.drop_table("modules")
    op.drop_table("courses")

    # ── 5. Drop orphan enum types ──

    op.execute("DROP TYPE IF EXISTS source_type_enum")
    op.execute("DROP TYPE IF EXISTS processing_status_enum")


def downgrade() -> None:
    # Recreate enum types
    op.execute(
        "CREATE TYPE source_type_enum AS ENUM ('video', 'presentation', 'text', 'web')"
    )
    op.execute(
        "CREATE TYPE processing_status_enum "
        "AS ENUM ('pending', 'processing', 'done', 'error')"
    )

    # Recreate courses table
    op.create_table(
        "courses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Recreate modules
    op.create_table(
        "modules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Recreate source_materials
    op.create_table(
        "source_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_type",
            postgresql.ENUM(
                "video",
                "presentation",
                "text",
                "web",
                name="source_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("filename", sa.String(500), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "processing",
                "done",
                "error",
                name="processing_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("content_snapshot", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Recreate lessons
    op.create_table(
        "lessons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("video_start_timecode", sa.String(20), nullable=True),
        sa.Column("video_end_timecode", sa.String(20), nullable=True),
        sa.Column("slide_range", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Recreate concepts
    op.create_table(
        "concepts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("examples", postgresql.JSONB(), nullable=True),
        sa.Column("timecodes", postgresql.JSONB(), nullable=True),
        sa.Column("slide_references", postgresql.JSONB(), nullable=True),
        sa.Column("web_references", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Recreate exercises
    op.create_table(
        "exercises",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reference_solution", sa.Text(), nullable=True),
        sa.Column("grading_criteria", sa.Text(), nullable=True),
        sa.Column("difficulty_level", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Re-add course_id to material_nodes
    op.add_column(
        "material_nodes",
        sa.Column("course_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "material_nodes_course_id_fkey",
        "material_nodes",
        "courses",
        ["course_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_material_nodes_course_id", "material_nodes", ["course_id"])

    # Re-add course_id to structure_snapshots
    op.add_column(
        "structure_snapshots",
        sa.Column("course_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "course_structure_snapshots_course_id_fkey",
        "structure_snapshots",
        "courses",
        ["course_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_snapshots_course", "structure_snapshots", ["course_id"])

    # Recreate unique index with course_id
    op.drop_index("uq_snapshots_identity", table_name="structure_snapshots")
    op.create_index(
        "uq_snapshots_identity",
        "structure_snapshots",
        [
            "course_id",
            sa.text("COALESCE(node_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "node_fingerprint",
            "mode",
        ],
        unique=True,
    )

    # Re-add course_id to jobs, drop tenant_id
    op.add_column("jobs", sa.Column("course_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "jobs_course_id_fkey",
        "jobs",
        "courses",
        ["course_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_jobs_course_id", "jobs", ["course_id"])

    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_constraint("fk_jobs_tenant", "jobs", type_="foreignkey")
    op.drop_column("jobs", "tenant_id")
