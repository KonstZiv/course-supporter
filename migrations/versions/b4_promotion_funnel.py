"""Б-4: promotion paths — the tenant's path, the signup's source tag, enrollment events.

Revision ID: b4_promotion_funnel
Revises: v1_author_interface_language
Create Date: 2026-11-26

The product owner measures which promotion path brings students and money:
the platform's own courses, partner schools (legal persons), authors who are
natural persons. Three things are added:

- ``tenants.promotion_path`` — nullable ``String(20)``, CHECK ``own_course`` |
  ``school`` | ``author``; NULL = not set. Nothing is backfilled: the
  administrator sets it (``scripts/billing_admin.py set-promotion-path``).
- ``portal_signups.source_tag`` — nullable ``String(40)``, CHECK
  ``^[a-z0-9_-]{1,40}$``: the campaign tag of the join link (``?src=``).
- ``enrollment_events`` — insert-only enrollment facts (the
  ``billing_refuse_update`` trigger from ``bila_billing_accounts`` refuses
  UPDATE). ``student_enrollments`` loses its rows when a grant is withdrawn
  or the student is deleted; the events stay. ``student_id`` has no ON DELETE
  action; tenant and course cascade, as on the grant.

Backfill: one event per existing ``student_enrollments`` row — the same id,
its ``created_at`` and ``source``, the student's tenant, ``source_tag`` NULL.

Downgrade drops the table and both columns (their checks go with them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4_promotion_funnel"
down_revision = "v1_author_interface_language"
branch_labels = None
depends_on = None

_TAG_CHECK = "source_tag ~ '^[a-z0-9_-]{1,40}$'"
_INSERT_ONLY_FUNCTION = "billing_refuse_update"  # created by bila_billing_accounts
_EVENTS = "enrollment_events"


def _promotion_path() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "promotion_path",
            sa.String(length=20),
            nullable=True,
            comment=(
                "Promotion path the tenant belongs to (Б-4): own_course — the "
                "platform's own courses, school — a partner school (a legal "
                "person), author — an author who is a natural person. NULL = "
                "not set. Set by scripts/billing_admin.py set-promotion-path; "
                "read by the funnel. CHECK-guarded."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_tenants_promotion_path",
        "tenants",
        "promotion_path IN ('own_course', 'school', 'author')",
    )


def _signup_source_tag() -> None:
    op.add_column(
        "portal_signups",
        sa.Column(
            "source_tag",
            sa.String(length=40),
            nullable=True,
            comment=(
                "Campaign tag of the page the visitor came from (the src "
                "parameter of the join link, Б-4): a-z, 0-9, '-', '_', 1-40 "
                "chars (CHECK). NULL = none, or an invalid one ignored at "
                "signup. Copied to the enrollment event at confirm."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_portal_signups_source_tag", "portal_signups", _TAG_CHECK
    )


def _events() -> None:
    op.create_table(
        _EVENTS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → the tenant whose course was joined.",
        ),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            sa.ForeignKey("course_nodes.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → root CourseNode (the course).",
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("students.id"),
            nullable=False,
            comment=(
                "FK → Student, no ON DELETE action: a deleted student stays "
                "as an anonymized row and the event keeps pointing at it."
            ),
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment=(
                "Who made the grant, as student_enrollments.source: 'author' "
                "or 'self'. CHECK-guarded."
            ),
        ),
        sa.Column(
            "source_tag",
            sa.String(length=40),
            nullable=True,
            comment=(
                "Campaign tag of a self-enrollment (the join link's src), "
                "same rule as portal_signups.source_tag. NULL = none or an "
                "author's grant."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the student was enrolled; the funnel's period reads it.",
        ),
        sa.CheckConstraint(
            "source IN ('author', 'self')", name="ck_enrollment_events_source"
        ),
        sa.CheckConstraint(_TAG_CHECK, name="ck_enrollment_events_source_tag"),
        comment=(
            "Enrollment facts for the promotion funnel (Б-4); insert-only, "
            "outlive the grant and the student's deletion"
        ),
    )
    for column in ("tenant_id", "course_node_id", "student_id", "created_at"):
        op.create_index(f"ix_{_EVENTS}_{column}", _EVENTS, [column])
    # The function name is a constant above, never input.
    op.execute(
        f"CREATE TRIGGER {_EVENTS}_insert_only BEFORE UPDATE ON {_EVENTS} "
        f"FOR EACH ROW EXECUTE FUNCTION {_INSERT_ONLY_FUNCTION}();"
    )
    op.execute(
        f"""
INSERT INTO {_EVENTS}
    (id, tenant_id, course_node_id, student_id, source, source_tag, created_at)
SELECT e.id, s.tenant_id, e.course_node_id, e.student_id, e.source, NULL,
       e.created_at
FROM student_enrollments AS e
JOIN students AS s ON s.id = e.student_id
"""
    )


def upgrade() -> None:
    """Add the path, the source tag and the enrollment events (backfilled)."""
    _promotion_path()
    _signup_source_tag()
    _events()


def downgrade() -> None:
    """Drop the events table and both columns."""
    op.drop_table(_EVENTS)
    op.drop_column("portal_signups", "source_tag")
    op.drop_column("tenants", "promotion_path")
