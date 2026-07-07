"""KD18 P2: project base-archive versions + submission snapshot carriers.

Revision ID: project_bases_p2
Revises: pwrecovery_r2_tokens
Create Date: 2026-07-07 00:00:00.000000

The single, forward-only migration of the project-tasks "base + delta" cycle
(KD18). Adds the base life-cycle carrier and the submission-side snapshot
columns that P3 will populate WITHOUT a second migration:

  - ``project_bases`` — append-only normalized base-archive versions, one row
    per upload version of a project task's base. ``authored_document_id`` FK
    CASCADE (delete the task → drop its bases). ``snapshot_key`` / ``snapshot_
    hash`` / ``manifest`` are NULL until the base-normalize ARQ job flips
    ``state`` pending → ready; ``failure_reason`` carries the human-readable
    reason on ``state = 'failed'`` (CHECK-guarded to pending|ready|failed).
    ``snapshot_hash`` is indexed (non-unique) for the echo-match WHERE. The
    composite ``(authored_document_id, version)`` unique index enforces one row
    per version and covers the FK cascade (leftmost prefix), so no standalone
    FK index.
  - ``homework_submissions`` gains four NULLABLE columns — ``base_id`` (FK →
    project_bases, ON DELETE SET NULL), ``snapshot_key``, ``snapshot_hash``,
    ``snapshot_manifest`` (JSONB). P2 only lands them; P3 fills them. Nullable
    with NO server_default is the P3 no-second-migration contract.

Forward-only: ``project_bases`` starts empty and the four submission columns
are nullable adds (no backfill). Downgrade drops the four columns, the FK, and
the table for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "project_bases_p2"
down_revision = "pwrecovery_r2_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create project_bases + add the four submission snapshot columns."""
    op.create_table(
        "project_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the project task). CASCADE. The "
                "composite (authored_document_id, version) unique index covers "
                "this FK (leftmost prefix), so no standalone FK index."
            ),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment=(
                "Monotonic 1-based version per document. Re-upload = new "
                "version; existing submissions retain their referenced "
                "version. Active version = latest READY."
            ),
        ),
        sa.Column(
            "archive_key",
            sa.String(length=2000),
            nullable=False,
            comment="S3 object key of the raw uploaded base archive (original).",
        ),
        sa.Column(
            "snapshot_key",
            sa.String(length=2000),
            nullable=True,
            comment="S3 object key of the normalized canonical zip. NULL until READY.",
        ),
        sa.Column(
            "snapshot_hash",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Aggregate SHA-256 (sorted path:hash) of the normalized "
                "snapshot — the echo-match key. NULL until READY. Indexed "
                "(non-unique) for the WHERE-match."
            ),
        ),
        sa.Column(
            "manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Algorithmic manifest (schema / aggregate_hash / included / "
                "excluded / totals) of the normalized snapshot. NULL until READY."
            ),
        ),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
            comment=(
                "Normalization lifecycle: pending → ready | failed(reason). "
                "Enforced by ck_project_bases_state."
            ),
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment=(
                "Human-readable failure reason when state='failed' (the "
                "SecurityRejectedError category / NormalizerError message)."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'failed')",
            name="ck_project_bases_state",
        ),
        sa.ForeignKeyConstraint(
            ["authored_document_id"],
            ["authored_documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Append-only normalized base-archive versions for project tasks (KD18)",
    )
    op.create_index(
        "ix_project_bases_snapshot_hash",
        "project_bases",
        ["snapshot_hash"],
        unique=False,
    )
    op.create_index(
        "uq_project_base_document_version",
        "project_bases",
        ["authored_document_id", "version"],
        unique=True,
    )

    op.add_column(
        "homework_submissions",
        sa.Column(
            "base_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "FK → ProjectBase version this project submission was diffed "
                "against (KD18). SET NULL. NULL for non-project submissions. "
                "Populated by P3."
            ),
        ),
    )
    op.add_column(
        "homework_submissions",
        sa.Column(
            "snapshot_key",
            sa.String(length=2000),
            nullable=True,
            comment=(
                "S3 object key of the submission's normalized snapshot zip "
                "(KD18). NULL for non-project submissions. Populated by P3."
            ),
        ),
    )
    op.add_column(
        "homework_submissions",
        sa.Column(
            "snapshot_hash",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Aggregate SHA-256 of the submission snapshot (KD18) — the "
                "echoed base_snapshot_hash match key. NULL for non-project "
                "submissions. Populated by P3."
            ),
        ),
    )
    op.add_column(
        "homework_submissions",
        sa.Column(
            "snapshot_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Algorithmic manifest of the submission snapshot (KD18). NULL "
                "for non-project submissions. Populated by P3."
            ),
        ),
    )
    op.create_foreign_key(
        "fk_homework_submissions_base_id",
        "homework_submissions",
        "project_bases",
        ["base_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_homework_submissions_base_id",
        "homework_submissions",
        ["base_id"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse: drop the four submission columns, the FK, and project_bases."""
    op.drop_index(
        "ix_homework_submissions_base_id",
        table_name="homework_submissions",
    )
    op.drop_constraint(
        "fk_homework_submissions_base_id",
        "homework_submissions",
        type_="foreignkey",
    )
    op.drop_column("homework_submissions", "snapshot_manifest")
    op.drop_column("homework_submissions", "snapshot_hash")
    op.drop_column("homework_submissions", "snapshot_key")
    op.drop_column("homework_submissions", "base_id")

    op.drop_index(
        "uq_project_base_document_version",
        table_name="project_bases",
    )
    op.drop_index(
        "ix_project_bases_snapshot_hash",
        table_name="project_bases",
    )
    op.drop_table("project_bases")
