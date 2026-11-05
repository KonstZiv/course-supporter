"""BIL-A: payer accounts, payment rules, author settings, consent, audit.

Revision ID: bila_billing_accounts
Revises: bild_action_cost_estimates
Create Date: 2026-11-05

Creates five tables:

* ``billing_accounts`` — a prepaid account of a student or of an author (the
  tenant); at most one active account per owner (two partial unique indexes);
* ``billing_payment_rules`` — "someone else pays" for a kind of action on a
  course or for a student; at most one active rule per (target, action kind);
* ``billing_author_settings`` — benefit and credit of an author account;
* ``billing_consents`` — consent to a revision of the billing terms;
* ``billing_audit_log`` — every administrative change, before and after.

No table has a foreign key to a table outside billing (tenants, students,
course_nodes): the owner ids are plain indexed columns, so deleting a tenant
or a student never reaches a billing row and never fails on one — the lesson
of ``action_cost_estimates`` (BIL-D), where several ``ON DELETE SET NULL``
paths to one row failed the deletion of the account. Foreign keys inside
billing point at ``billing_accounts`` with no ON DELETE action; nothing outside
billing deletes an account.

``billing_consents`` and ``billing_audit_log`` are insert-only: one trigger
function refuses an UPDATE on either.

Column and table comments are copied from the ORM (``storage.orm.Billing*``).
Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).
New tables rewrite no row. The downgrade drops them; lossless ONLY on a
database where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bila_billing_accounts"
down_revision: str | Sequence[str] | None = "bild_action_cost_estimates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSERT_ONLY_FUNCTION = "billing_refuse_update"
_INSERT_ONLY_TABLES = ("billing_consents", "billing_audit_log")


def _created_at(comment: str) -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        comment=comment,
    )


def _accounts() -> None:
    op.create_table(
        "billing_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="student (pays for the reviews of its own work) | author (pays for processing materials and preparing tasks).",
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=True,
            comment="Tenant the account belongs to (the author is the tenant). No FK. NULL once anonymized.",
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            nullable=True,
            comment="Student of a student account; NULL on an author account. No FK. NULL once anonymized.",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
            comment="active | closed. Closed only when the owner is deleted.",
        ),
        _created_at("When the account was created (lazily, on first need)."),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the account was closed; NULL while active.",
        ),
        sa.Column(
            "anonymized_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the owner references were erased after the owner's deletion; the start of the retention period.",
        ),
        sa.CheckConstraint(
            "kind IN ('student', 'author')", name="ck_billing_accounts_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'closed')", name="ck_billing_accounts_status"
        ),
        sa.CheckConstraint(
            "kind = 'student' OR student_id IS NULL",
            name="ck_billing_accounts_author_has_no_student",
        ),
        sa.CheckConstraint(
            "anonymized_at IS NOT NULL OR (tenant_id IS NOT NULL "
            "AND (kind = 'author' OR student_id IS NOT NULL))",
            name="ck_billing_accounts_owner_until_anonymized",
        ),
        sa.CheckConstraint(
            "anonymized_at IS NULL OR (status = 'closed' "
            "AND tenant_id IS NULL AND student_id IS NULL)",
            name="ck_billing_accounts_anonymized_is_closed",
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="ck_billing_accounts_closed_at",
        ),
        comment="Prepaid account of a student (reviews) or an author (processing, task preparation) (BIL-A). Never deleted with its owner: closed and anonymized instead.",
    )
    op.create_index("ix_billing_accounts_tenant_id", "billing_accounts", ["tenant_id"])
    op.create_index(
        "ix_billing_accounts_student_id", "billing_accounts", ["student_id"]
    )
    op.create_index(
        "uq_billing_accounts_active_author",
        "billing_accounts",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'author' AND status = 'active'"),
    )
    op.create_index(
        "uq_billing_accounts_active_student",
        "billing_accounts",
        ["tenant_id", "student_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'student' AND status = 'active'"),
    )


def _payment_rules() -> None:
    op.create_table(
        "billing_payment_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "scope",
            sa.String(16),
            nullable=False,
            comment="course | student: what the rule is about.",
        ),
        sa.Column(
            "action_kind",
            sa.String(32),
            nullable=False,
            comment="review | processing | task_preparation.",
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Tenant of the course or the student. No FK.",
        ),
        sa.Column(
            "course_node_id",
            sa.Uuid(),
            nullable=True,
            comment="Root CourseNode (the course) of a course rule. No FK.",
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            nullable=True,
            comment="Student of a student rule. No FK.",
        ),
        sa.Column(
            "payer_account_id",
            sa.Uuid(),
            sa.ForeignKey("billing_accounts.id"),
            nullable=False,
            comment="The account that pays: the author account of the same tenant.",
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="True until the rule is ended.",
        ),
        _created_at("When the rule was set."),
        sa.Column(
            "created_by",
            sa.String(200),
            nullable=False,
            comment="Who set the rule (the administrator's --actor).",
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the rule was ended; NULL while active.",
        ),
        sa.CheckConstraint(
            "scope IN ('course', 'student')", name="ck_billing_payment_rules_scope"
        ),
        sa.CheckConstraint(
            "action_kind IN ('review', 'processing', 'task_preparation')",
            name="ck_billing_payment_rules_action_kind",
        ),
        sa.CheckConstraint(
            "(scope = 'course') = (course_node_id IS NOT NULL) "
            "AND (scope = 'student') = (student_id IS NOT NULL)",
            name="ck_billing_payment_rules_target",
        ),
        sa.CheckConstraint(
            "active = (ended_at IS NULL)", name="ck_billing_payment_rules_active"
        ),
        comment="Who pays for a kind of action on a course or for a student, instead of the default (BIL-A). Set by the administrator.",
    )
    for column in ("tenant_id", "course_node_id", "student_id", "payer_account_id"):
        op.create_index(
            f"ix_billing_payment_rules_{column}", "billing_payment_rules", [column]
        )
    op.create_index(
        "uq_billing_payment_rules_active_course",
        "billing_payment_rules",
        ["course_node_id", "action_kind"],
        unique=True,
        postgresql_where=sa.text("active AND scope = 'course'"),
    )
    op.create_index(
        "uq_billing_payment_rules_active_student",
        "billing_payment_rules",
        ["student_id", "action_kind"],
        unique=True,
        postgresql_where=sa.text("active AND scope = 'student'"),
    )


def _author_settings() -> None:
    op.create_table(
        "billing_author_settings",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("billing_accounts.id"),
            primary_key=True,
            comment="The author account these settings are of.",
        ),
        sa.Column(
            "benefit",
            sa.String(16),
            nullable=False,
            server_default="none",
            comment="none | monthly_limit (free up to monthly_limit_units a month) | unlimited.",
        ),
        sa.Column(
            "monthly_limit_units",
            sa.Integer(),
            nullable=True,
            comment="Units a month free of charge; only with monthly_limit.",
        ),
        sa.Column(
            "benefit_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the current kind of benefit was granted; NULL with none.",
        ),
        sa.Column(
            "credit_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="May the account go below zero.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the settings last changed.",
        ),
        sa.CheckConstraint(
            "benefit IN ('none', 'monthly_limit', 'unlimited')",
            name="ck_billing_author_settings_benefit",
        ),
        sa.CheckConstraint(
            "(benefit = 'monthly_limit') = (monthly_limit_units IS NOT NULL)",
            name="ck_billing_author_settings_limit_needs_benefit",
        ),
        sa.CheckConstraint(
            "monthly_limit_units IS NULL OR monthly_limit_units > 0",
            name="ck_billing_author_settings_limit_positive",
        ),
        sa.CheckConstraint(
            "(benefit = 'none') = (benefit_started_at IS NULL)",
            name="ck_billing_author_settings_benefit_started",
        ),
        comment="Benefit and credit of an author account, set by the administrator (BIL-A). No row = no benefit, no credit.",
    )


def _consents() -> None:
    op.create_table(
        "billing_consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("billing_accounts.id"),
            nullable=False,
            comment="The account whose holder agreed.",
        ),
        sa.Column(
            "terms_version",
            sa.String(32),
            nullable=False,
            comment="Revision of the terms agreed to (settings.billing_terms_version).",
        ),
        sa.Column(
            "consented_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the holder agreed.",
        ),
        sa.Column(
            "text_sha256",
            sa.String(64),
            nullable=False,
            comment="SHA-256 (hex) of the terms text the holder was shown, as the client reports it.",
        ),
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            comment="Where the holder agreed: portal.",
        ),
        sa.CheckConstraint(
            "text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_billing_consents_text_sha256",
        ),
        sa.CheckConstraint(
            "channel IN ('portal')", name="ck_billing_consents_channel"
        ),
        sa.UniqueConstraint(
            "account_id", "terms_version", name="uq_billing_consents_version"
        ),
        comment="Consent to the billing terms, one row per account and terms revision (BIL-A). Insert-only.",
    )
    op.create_index(
        "ix_billing_consents_account_id", "billing_consents", ["account_id"]
    )


def _audit_log() -> None:
    op.create_table(
        "billing_audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "actor",
            sa.String(200),
            nullable=False,
            comment="Who made the change (the administrator's --actor).",
        ),
        _created_at("When the change was made."),
        sa.Column(
            "action",
            sa.String(64),
            nullable=False,
            comment="What was done, e.g. author_settings.update, payment_rule.create, payment_rule.end, account.anonymize.",
        ),
        sa.Column(
            "entity",
            sa.String(32),
            nullable=False,
            comment="Kind of row changed: account | author_settings | payment_rule.",
        ),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            nullable=False,
            comment="Id of the row changed. No FK (several tables).",
        ),
        sa.Column(
            "before",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="The row before the change; NULL when it was created.",
        ),
        sa.Column(
            "after",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="The row after the change.",
        ),
        comment="Audit log of administrative changes to billing data (BIL-A): who, when, what, before and after. Insert-only.",
    )
    op.create_index(
        "ix_billing_audit_log_entity_id", "billing_audit_log", ["entity_id"]
    )


def _insert_only() -> None:
    # Table names come from the hardcoded tuple above, never from input.
    op.execute(
        f"""
CREATE OR REPLACE FUNCTION {_INSERT_ONLY_FUNCTION}()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '%.% is insert-only: UPDATE refused',
        TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""
    )
    for table in _INSERT_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_INSERT_ONLY_FUNCTION}();"
        )


def upgrade() -> None:
    """Create the five tables, their checks, indexes and the insert-only guard."""
    _accounts()
    _payment_rules()
    _author_settings()
    _consents()
    _audit_log()
    _insert_only()


def downgrade() -> None:
    """Drop the tables (indexes, constraints and triggers go with them)."""
    for table in (
        "billing_audit_log",
        "billing_consents",
        "billing_author_settings",
        "billing_payment_rules",
        "billing_accounts",
    ):
        op.drop_table(table)
    op.execute(f"DROP FUNCTION {_INSERT_ONLY_FUNCTION}()")
