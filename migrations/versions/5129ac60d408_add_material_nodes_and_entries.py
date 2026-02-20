"""add_material_nodes_and_entries

Revision ID: 5129ac60d408
Revises: 3bfcc509f45c
Create Date: 2026-02-20 15:02:27.817581

"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
import uuid_utils as uuid7_lib
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5129ac60d408"
down_revision: Union[str, Sequence[str], None] = "3bfcc509f45c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid7() -> uuid.UUID:
    """Generate a UUIDv7 for data migration."""
    return uuid.UUID(bytes=uuid7_lib.uuid7().bytes)


def upgrade() -> None:
    """Create material_nodes + material_entries tables, migrate source_materials data."""
    # ── 1. DDL: create tables ──
    op.create_table(
        "material_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("node_fingerprint", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["material_nodes.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_material_nodes_course_id"),
        "material_nodes",
        ["course_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_material_nodes_parent_id"),
        "material_nodes",
        ["parent_id"],
        unique=False,
    )
    op.create_table(
        "material_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
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
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=True),
        sa.Column("raw_hash", sa.String(length=64), nullable=True),
        sa.Column("raw_size_bytes", sa.Integer(), nullable=True),
        sa.Column("processed_hash", sa.String(length=64), nullable=True),
        sa.Column("processed_content", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_job_id", sa.Uuid(), nullable=True),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["node_id"], ["material_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pending_job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_material_entries_node_id"),
        "material_entries",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_material_entries_pending_job_id"),
        "material_entries",
        ["pending_job_id"],
        unique=False,
    )

    # ── 2. Data migration: source_materials → material_nodes + material_entries ──
    conn = op.get_bind()

    # Get distinct course_ids that have source_materials
    courses_with_materials = conn.execute(
        sa.text("SELECT DISTINCT course_id FROM source_materials")
    ).fetchall()

    for (course_id,) in courses_with_materials:
        # Create a root MaterialNode for each course
        root_node_id = _uuid7()
        conn.execute(
            sa.text(
                """
                INSERT INTO material_nodes (id, course_id, parent_id, title, "order")
                VALUES (:id, :course_id, NULL, :title, 0)
                """
            ),
            {
                "id": root_node_id,
                "course_id": course_id,
                "title": "Imported Materials",
            },
        )

        # Migrate source_materials to material_entries under the root node
        materials = conn.execute(
            sa.text(
                """
                SELECT id, source_type, source_url, filename,
                       content_snapshot, processed_at, error_message, created_at
                FROM source_materials
                WHERE course_id = :course_id
                ORDER BY created_at
                """
            ),
            {"course_id": course_id},
        ).fetchall()

        for idx, mat in enumerate(materials):
            entry_id = _uuid7()
            # Map SourceMaterial fields to MaterialEntry fields:
            # - content_snapshot → processed_content
            # - status done/error → derived from fields
            conn.execute(
                sa.text(
                    """
                    INSERT INTO material_entries
                        (id, node_id, source_type, "order", source_url,
                         filename, processed_content, processed_at,
                         error_message, created_at)
                    VALUES
                        (:id, :node_id, :source_type, :order, :source_url,
                         :filename, :processed_content, :processed_at,
                         :error_message, :created_at)
                    """
                ),
                {
                    "id": entry_id,
                    "node_id": root_node_id,
                    "source_type": mat.source_type,
                    "order": idx,
                    "source_url": mat.source_url,
                    "filename": mat.filename,
                    "processed_content": mat.content_snapshot,
                    "processed_at": mat.processed_at,
                    "error_message": mat.error_message,
                    "created_at": mat.created_at,
                },
            )


def downgrade() -> None:
    """Drop material_entries and material_nodes tables.

    Data migration is not reversed — source_materials remains untouched.
    """
    op.drop_index(
        op.f("ix_material_entries_pending_job_id"), table_name="material_entries"
    )
    op.drop_index(op.f("ix_material_entries_node_id"), table_name="material_entries")
    op.drop_table("material_entries")
    op.drop_index(op.f("ix_material_nodes_parent_id"), table_name="material_nodes")
    op.drop_index(op.f("ix_material_nodes_course_id"), table_name="material_nodes")
    op.drop_table("material_nodes")
