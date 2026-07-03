"""Phase 6 R2: student portal password-recovery token layer.

Revision ID: pwrecovery_r2_tokens
Revises: portal_t3_slide_keys
Create Date: 2026-07-03 00:00:00.000000

Adds the self-service password-recovery carriers (sprint-password-recovery R2):

  - ``student_credentials.recovery_email`` + ``recovery_email_confirmed_at`` —
    two nullable columns for the optional student-owned recovery address (Q2).
    The student sets and confirms it in the portal; the tenant does not supply
    it. Pending = email set + confirmed NULL; changing the email resets
    confirmed. A forgot-password link is only ever sent to a confirmed address.
  - ``student_credential_tokens`` — single-use recovery tokens (Q3), keyed by
    ``purpose`` (``password_reset`` | ``email_confirm``, CHECK-guarded). Only
    the SHA-256 hash of the raw token is stored (``token_hash``, unique). Single
    use is enforced by ``used_at``; ``ON DELETE CASCADE`` from the credential
    removes tokens with it. The composite ``(credential_id, purpose)`` index
    covers both the burn-unused-siblings query and the FK cascade.

Forward-only: the two columns are nullable adds (no backfill) and the new table
starts empty. Plain table (no soft-delete) — tokens are transient.

Downgrade drops the table and the two columns for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pwrecovery_r2_tokens"
down_revision = "portal_t3_slide_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add recovery-email columns + create student_credential_tokens."""
    op.add_column(
        "student_credentials",
        sa.Column(
            "recovery_email",
            sa.String(length=320),
            nullable=True,
            comment=(
                "Optional student-owned recovery email (Phase 6 R2). The student "
                "sets and confirms it in the portal; the tenant does not supply "
                "it. NULL = none set. A forgot-password link is only ever sent "
                "to a CONFIRMED address (recovery_email_confirmed_at NOT NULL)."
            ),
        ),
    )
    op.add_column(
        "student_credentials",
        sa.Column(
            "recovery_email_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the recovery_email was confirmed (Phase 6 R2). NULL while "
                "pending; changing recovery_email resets this to NULL."
            ),
        ),
    )

    op.create_table(
        "student_credential_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "credential_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → StudentCredential the token belongs to. The composite "
                "(credential_id, purpose) index covers both the burn-siblings "
                "query and the FK cascade (leftmost prefix), so no standalone "
                "FK index."
            ),
        ),
        sa.Column(
            "purpose",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Token flow: 'password_reset' or 'email_confirm' (CHECK-guarded)."
            ),
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 hex of the raw token (hash_api_key). The redemption "
                "lookup is by hash (unique index); the raw token is never stored."
            ),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Expiry (issue time + purpose TTL: reset short, confirm long).",
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When redeemed (single-use). NULL = unused. Issuing a new token "
                "of the same purpose also stamps outstanding siblings."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('password_reset', 'email_confirm')",
            name="ck_student_credential_token_purpose",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["student_credentials.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Single-use recovery token (password_reset / email_confirm)",
    )
    op.create_index(
        "uq_student_credential_token_hash",
        "student_credential_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_student_credential_tokens_credential_purpose",
        "student_credential_tokens",
        ["credential_id", "purpose"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse: drop the token table and the two recovery-email columns."""
    op.drop_index(
        "ix_student_credential_tokens_credential_purpose",
        table_name="student_credential_tokens",
    )
    op.drop_index(
        "uq_student_credential_token_hash",
        table_name="student_credential_tokens",
    )
    op.drop_table("student_credential_tokens")

    op.drop_column("student_credentials", "recovery_email_confirmed_at")
    op.drop_column("student_credentials", "recovery_email")
