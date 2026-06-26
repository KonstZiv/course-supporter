"""Phase 6 T1: student portal identity & access.

Revision ID: portal_t1_identity
Revises: mentor_t7_sanity_result
Create Date: 2026-06-26 00:00:00.000000

Adds the student-portal identity layer (KD17 §3 + §5 Phase 6):

  - ``students.display_name`` — nullable typed column for the portal's
    own-facing name (chosen over ``metadata_``). Integration-mode students
    (created via ``get_or_create`` on submit) carry none.
  - ``student_credentials`` — native portal login, 1:1-optional with
    ``Student`` (login + argon2id hash + ``is_active``). ``is_active=False``
    is "revoke access"; ``Student`` and history are retained (DD-6-A).
    ``login`` is unique per tenant (composite unique ``(tenant_id, login)``).
  - ``student_enrollments`` — student↔course access grant (our side is the
    source of truth); unbind is a row DELETE.

Forward-only: both new tables start empty and ``display_name`` is a nullable
add (no backfill). Plain tables (no soft-delete) — the credential's lifecycle
is ``is_active`` and the enrollment's is DELETE-on-unbind, so neither joins the
cascade-soft-delete machinery; the ``Student → HomeworkSubmission`` cascade is
untouched. Only the three intended operations are emitted — the unrelated
comment / index drift Alembic autogenerate surfaces on pre-existing tables is
the standing autogenerate-drift backlog item, not T1's to resolve.

Downgrade drops the two tables and the column for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "portal_t1_identity"
down_revision = "mentor_t7_sanity_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add display_name + create student_credentials & student_enrollments."""
    op.add_column(
        "students",
        sa.Column(
            "display_name",
            sa.String(length=200),
            nullable=True,
            comment=(
                "Portal display name (Phase 6 T1, KD17). Typed column — chosen "
                "over metadata_ — for the student's own-facing name in the "
                "portal. Nullable: integration-mode students (created via "
                "get_or_create on submit) carry none."
            ),
        ),
    )

    op.create_table(
        "student_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "student_id",
            sa.Uuid(),
            nullable=False,
            comment="FK → Student (UNIQUE — 1:1-optional credential per KD17)",
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "Owning tenant. Present here (not derived) because login lookup "
                "is by (tenant_id, login) before the student is known."
            ),
        ),
        sa.Column(
            "login",
            sa.String(length=200),
            nullable=False,
            comment=(
                "Student login, unique per tenant (composite unique with tenant_id)."
            ),
        ),
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=False,
            comment=(
                "Argon2id password hash (argon2-cffi). Raw password is never "
                "stored and shown only once at provisioning."
            ),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            comment=(
                "False = access revoked (login + existing bearer tokens "
                "rejected at the per-request is_active check). Student and "
                "history are retained (DD-6-A)."
            ),
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
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Native portal login credential, 1:1-optional with Student",
    )
    op.create_index(
        op.f("ix_student_credentials_student_id"),
        "student_credentials",
        ["student_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_student_credentials_tenant_id"),
        "student_credentials",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "uq_student_credential_tenant_login",
        "student_credentials",
        ["tenant_id", "login"],
        unique=True,
    )

    op.create_table(
        "student_enrollments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "student_id",
            sa.Uuid(),
            nullable=False,
            comment="FK → Student.",
        ),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → root CourseNode (the course). Root-ness "
                "(parent_id IS NULL) is validated at the bind route, not the FK."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_node_id"], ["course_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Student↔course access grant (KD17); unbind = row DELETE",
    )
    op.create_index(
        op.f("ix_student_enrollments_course_node_id"),
        "student_enrollments",
        ["course_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_student_enrollments_student_id"),
        "student_enrollments",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        "uq_student_enrollment",
        "student_enrollments",
        ["student_id", "course_node_id"],
        unique=True,
    )


def downgrade() -> None:
    """Reverse: drop the two tables and the column (round-trip only)."""
    op.drop_index("uq_student_enrollment", table_name="student_enrollments")
    op.drop_index(
        op.f("ix_student_enrollments_student_id"), table_name="student_enrollments"
    )
    op.drop_index(
        op.f("ix_student_enrollments_course_node_id"), table_name="student_enrollments"
    )
    op.drop_table("student_enrollments")

    op.drop_index(
        "uq_student_credential_tenant_login", table_name="student_credentials"
    )
    op.drop_index(
        op.f("ix_student_credentials_tenant_id"), table_name="student_credentials"
    )
    op.drop_index(
        op.f("ix_student_credentials_student_id"), table_name="student_credentials"
    )
    op.drop_table("student_credentials")

    op.drop_column("students", "display_name")
