"""add homework models (students, submissions, tenant webhook)

Revision ID: ec8afe68a0e6
Revises: e5f6a7b8c9d0
Create Date: 2026-04-08 14:57:47.246464

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ec8afe68a0e6"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add students, homework_submissions tables and tenant webhook_url."""
    # 1. Students table
    op.create_table(
        "students",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "external_id",
            sa.String(length=200),
            nullable=False,
            comment="Student identifier from the caller's system",
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Arbitrary caller-provided metadata about the student",
        ),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="External students submitting homework",
    )
    op.create_index(
        op.f("ix_students_tenant_id"), "students", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_student_tenant_external",
        "students",
        ["tenant_id", "external_id"],
        unique=True,
    )

    # 2. Homework submissions table
    op.create_table(
        "homework_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            nullable=False,
            comment="Root MaterialNode representing the course",
        ),
        sa.Column(
            "node_id",
            sa.Uuid(),
            nullable=False,
            comment="Specific course node the submission targets",
        ),
        sa.Column(
            "task_hint_id",
            sa.Uuid(),
            nullable=True,
            comment="Optional task ID hint from the caller (not FK, validated in app)",
        ),
        sa.Column(
            "matched_task_id",
            sa.Uuid(),
            nullable=True,
            comment="Matched editable node after task identification",
        ),
        sa.Column(
            "file_url",
            sa.String(length=2000),
            nullable=False,
            comment="S3/B2 path to the uploaded file",
        ),
        sa.Column(
            "file_type",
            sa.String(length=50),
            nullable=False,
            comment="MIME type of the uploaded file",
        ),
        sa.Column("original_filename", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            comment="Lifecycle: received → safety_check → matching → matched → reviewing → completed → delivered | rejected | failed",
        ),
        sa.Column(
            "safety_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Safety check result (safe, reason, flags)",
        ),
        sa.Column(
            "match_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Task matching result (matched_node_id, task_type, confidence)",
        ),
        sa.Column(
            "review_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Mentor review result (score, passed, feedback_sections)",
        ),
        sa.Column(
            "review_markdown",
            sa.Text(),
            nullable=True,
            comment="Rendered Markdown of the Mentor review",
        ),
        sa.Column(
            "webhook_url",
            sa.String(length=2000),
            nullable=True,
            comment="Per-submission webhook URL override (falls back to tenant default)",
        ),
        sa.Column("webhook_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["course_node_id"], ["material_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["matched_task_id"], ["structure_nodes_editable.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["material_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Homework submissions from external systems for Mentor review",
    )
    op.create_index(
        op.f("ix_homework_submissions_course_node_id"),
        "homework_submissions",
        ["course_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_job_id"),
        "homework_submissions",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_matched_task_id"),
        "homework_submissions",
        ["matched_task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_node_id"),
        "homework_submissions",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_status"),
        "homework_submissions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_student_id"),
        "homework_submissions",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_homework_submissions_tenant_id"),
        "homework_submissions",
        ["tenant_id"],
        unique=False,
    )

    # 3. Tenant webhook_url
    op.add_column(
        "tenants",
        sa.Column(
            "webhook_url",
            sa.String(length=2000),
            nullable=True,
            comment="Default webhook URL for homework submission results",
        ),
    )


def downgrade() -> None:
    """Remove homework models."""
    op.drop_column("tenants", "webhook_url")
    op.drop_index(
        op.f("ix_homework_submissions_tenant_id"), table_name="homework_submissions"
    )
    op.drop_index(
        op.f("ix_homework_submissions_student_id"), table_name="homework_submissions"
    )
    op.drop_index(
        op.f("ix_homework_submissions_status"), table_name="homework_submissions"
    )
    op.drop_index(
        op.f("ix_homework_submissions_node_id"), table_name="homework_submissions"
    )
    op.drop_index(
        op.f("ix_homework_submissions_matched_task_id"),
        table_name="homework_submissions",
    )
    op.drop_index(
        op.f("ix_homework_submissions_job_id"), table_name="homework_submissions"
    )
    op.drop_index(
        op.f("ix_homework_submissions_course_node_id"),
        table_name="homework_submissions",
    )
    op.drop_table("homework_submissions")
    op.drop_index("uq_student_tenant_external", table_name="students")
    op.drop_index(op.f("ix_students_tenant_id"), table_name="students")
    op.drop_table("students")
