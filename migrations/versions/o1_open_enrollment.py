"""O-1: open courses — self-enrollment by a course link, confirmed by email.

Revision ID: o1_open_enrollment
Revises: biltest_student_flag
Create Date: 2026-11-23

A student enrolls in a course by the link the author gives out, confirms the
email, and only then gets the course (operator decisions R1-R9, 23.11). Adds
only; nothing existing is changed or filled in beyond the new column defaults:

  - ``course_nodes.enroll_key`` — TEXT NULL: the key of the course's link
    (``{portal_base_url}/{tenant_id}/join/{key}``), a random URL-safe secret of
    at least 128 bits. Unique (``uq_course_nodes_enroll_key``; NULLs do not
    collide) and allowed on a root node only
    (``ck_course_nodes_enroll_key_root``). NULL = enrollment closed — every
    existing course starts closed.
  - ``student_enrollments.source`` — TEXT NOT NULL DEFAULT 'author', CHECK IN
    ('author', 'self'): who made the grant. Every existing enrollment reads
    'author' (the author made all of them).
  - ``students.adult_confirmed_at`` — TIMESTAMPTZ NULL: when the student marked
    "I am 18 or older". NULL for every existing student.
  - ``portal_signups`` — a signup waiting for email confirmation. No student,
    credential, enrollment or billing account exists until confirm. Only the
    SHA-256 of the confirm token is kept (unique); one pending signup per
    (tenant, login) (partial unique index ``WHERE confirmed_at IS NULL``).
  - ``ix_student_credentials_confirmed_email`` — partial index on
    ``lower(recovery_email) WHERE recovery_email_confirmed_at IS NOT NULL``
    for the login by email.

Column and table comments are copied from the ORM (``storage.orm``).
Downgrade mirrors the upgrade (round-trip integrity). Hand-written;
``--autogenerate`` is not used on this database (``DD-L4-A``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "o1_open_enrollment"
down_revision = "biltest_student_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the enrollment link, the enrollment source, the adult mark, the
    signup table and the confirmed-email index."""
    op.add_column(
        "course_nodes",
        sa.Column(
            "enroll_key",
            sa.Text(),
            nullable=True,
            comment=(
                "Key of the course's self-enrollment link (O-1): a random "
                "URL-safe secret of at least 128 bits in "
                "{portal_base_url}/{tenant_id}/join/{key}. Root nodes only "
                "(CHECK); unique. NULL = self-enrollment closed. The author "
                "opens, rotates and closes it; enrollments already made are not "
                "affected."
            ),
        ),
    )
    op.create_index(
        "uq_course_nodes_enroll_key", "course_nodes", ["enroll_key"], unique=True
    )
    op.create_check_constraint(
        "ck_course_nodes_enroll_key_root",
        "course_nodes",
        "enroll_key IS NULL OR parent_id IS NULL",
    )

    op.add_column(
        "student_enrollments",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'author'"),
            comment=(
                "Who made the grant (O-1): 'author' — the author bound the "
                "student (POST /students/enrollments); 'self' — the student "
                "enrolled by the course's link. CHECK-guarded."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_student_enrollments_source",
        "student_enrollments",
        "source IN ('author', 'self')",
    )

    op.add_column(
        "students",
        sa.Column(
            "adult_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                'When the student marked "I am 18 or older" (O-1): at '
                "self-enrollment or when joining a course by its link. NULL = "
                "never marked (e.g. a student the author provisioned)."
            ),
        ),
    )

    op.create_table(
        "portal_signups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="The tenant (author) whose course is joined.",
        ),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → root CourseNode (the course) whose link was used. The "
                "link was checked at signup; confirm enrolls even if it is "
                "closed since."
            ),
        ),
        sa.Column(
            "login",
            sa.String(length=200),
            nullable=False,
            comment=(
                "The login asked for; becomes StudentCredential.login at "
                "confirm. Held by one pending request per tenant (partial "
                "unique index)."
            ),
        ),
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=False,
            comment=(
                "Argon2id hash of the password given (argon2-cffi); copied to "
                "the credential at confirm. The raw password is never stored."
            ),
        ),
        sa.Column(
            "email",
            sa.String(length=320),
            nullable=False,
            comment=(
                "The email given, lower-cased (CHECK). Becomes the confirmed "
                "recovery_email of the credential at confirm."
            ),
        ),
        sa.Column(
            "display_name",
            sa.String(length=200),
            nullable=True,
            comment="Portal display name given, if any.",
        ),
        sa.Column(
            "interface_language",
            sa.String(length=10),
            nullable=True,
            comment=(
                "Language of the portal pages chosen at signup: ISO 639-3, "
                "normalized on entry; picks the email language and becomes "
                "Student.interface_language at confirm. NULL = not chosen "
                "(Ukrainian email)."
            ),
        ),
        sa.Column(
            "adult_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                'When the visitor marked "I am 18 or older" (required); '
                "becomes Student.adult_confirmed_at at confirm."
            ),
        ),
        sa.Column(
            "terms_version",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Revision of the billing terms agreed to at signup (the one in "
                "force then); recorded as the student's consent at confirm."
            ),
        ),
        sa.Column(
            "text_sha256",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 (hex) of the terms text shown, as the client reports "
                "it; recorded with the consent at confirm."
            ),
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 hex of the raw confirm token (hash_api_key); the raw "
                "token lives only in the mailed link. Replaced on resend."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "created_at + 7 days; an unconfirmed request past it is void "
                "and deleted by the cleanup task. Resend does not extend it."
            ),
        ),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the email was confirmed and the student created. NULL = "
                "pending."
            ),
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            nullable=True,
            comment="FK → the Student created at confirm. NULL while pending.",
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_portal_signups_email_lower"),
        sa.CheckConstraint(
            "text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_portal_signups_text_sha256"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["course_node_id"], ["course_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "Self-enrollment request by a course link, waiting for email "
            "confirmation (O-1)"
        ),
    )
    op.create_index(
        "ix_portal_signups_tenant_id", "portal_signups", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_portal_signups_course_node_id",
        "portal_signups",
        ["course_node_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_signups_student_id", "portal_signups", ["student_id"], unique=False
    )
    op.create_index(
        "uq_portal_signups_token_hash", "portal_signups", ["token_hash"], unique=True
    )
    op.create_index(
        "uq_portal_signups_pending_login",
        "portal_signups",
        ["tenant_id", "login"],
        unique=True,
        postgresql_where=sa.text("confirmed_at IS NULL"),
    )

    op.create_index(
        "ix_student_credentials_confirmed_email",
        "student_credentials",
        [sa.text("lower(recovery_email)")],
        unique=False,
        postgresql_where=sa.text("recovery_email_confirmed_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Mirror of the upgrade."""
    op.drop_index(
        "ix_student_credentials_confirmed_email", table_name="student_credentials"
    )

    op.drop_index("uq_portal_signups_pending_login", table_name="portal_signups")
    op.drop_index("uq_portal_signups_token_hash", table_name="portal_signups")
    op.drop_index("ix_portal_signups_student_id", table_name="portal_signups")
    op.drop_index("ix_portal_signups_course_node_id", table_name="portal_signups")
    op.drop_index("ix_portal_signups_tenant_id", table_name="portal_signups")
    op.drop_table("portal_signups")

    op.drop_column("students", "adult_confirmed_at")

    op.drop_constraint(
        "ck_student_enrollments_source", "student_enrollments", type_="check"
    )
    op.drop_column("student_enrollments", "source")

    op.drop_constraint("ck_course_nodes_enroll_key_root", "course_nodes", type_="check")
    op.drop_index("uq_course_nodes_enroll_key", table_name="course_nodes")
    op.drop_column("course_nodes", "enroll_key")
