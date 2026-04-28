"""0.4 task: ExternalServiceCall — KD5-conformant minimal-FK shape

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-04-29 09:00:00.000000

Phase-0 task 0.4 (vision §3 KD5). Migrates ``external_service_calls``
to the canonical KD5 shape: ``job_id`` is the only mandatory FK; all
contextual attribution (tenant, course_node, authored_document) flows
through the ``Job`` join. Cost reports JOIN on ``jobs`` for tenant
filtering.

Steps (transactional — full rollback if any later step fails):

1. ``DELETE FROM external_service_calls WHERE job_id IS NULL`` —
   cleanup of pre-tracking orphan rows. Verified ~2685 rows in
   production at the time of (a) authoring; rows are unreachable
   from any tenant-scoped report (no Job → no tenant context) and
   discardable per task scope. The DELETE runs inside the same
   transaction as the schema changes; if any DDL below fails, the
   entire migration rolls back including the DELETE.

2. Drop FK ``external_service_calls_job_id_fkey`` and recreate with
   ``ON DELETE NO ACTION``. PostgreSQL does not support
   ``ALTER CONSTRAINT ... ON DELETE``; drop+recreate is the only
   path. Same constraint name preserved for downgrade symmetry.
   Per KD5: soft-deleted Jobs keep ``deleted_at`` and the FK stays
   valid; cost reports retain attribution to deleted materials.

3. ``ALTER COLUMN job_id SET NOT NULL`` — KD5 invariant.
   ContextVar-based plumbing in commit (a-pre) ensures every writer
   sets ``job_id`` before this migration runs.

4. Drop index ``ix_external_service_calls_tenant_id``.
5. Drop FK ``external_service_calls_tenant_id_fkey``.
6. ``DROP COLUMN tenant_id``.

**Prod-deploy advisory:** on tables with > 1M rows,
``ALTER COLUMN ... SET NOT NULL`` acquires ACCESS EXCLUSIVE during the
table scan. For zero-downtime, prefer the two-step approach::

    ALTER TABLE external_service_calls
        ADD CONSTRAINT esc_job_id_not_null
        CHECK (job_id IS NOT NULL) NOT VALID;
    ALTER TABLE external_service_calls
        VALIDATE CONSTRAINT esc_job_id_not_null;
    ALTER TABLE external_service_calls ALTER COLUMN job_id SET NOT NULL;
    ALTER TABLE external_service_calls DROP CONSTRAINT esc_job_id_not_null;

The ``CHECK ... NOT VALID`` takes only ROW EXCLUSIVE lock and is safe
under concurrent writes. ``VALIDATE`` then scans without blocking
writes; PostgreSQL 12+ uses the validated CHECK to skip the
``SET NOT NULL`` table scan, making it metadata-only. Production size
at the time of authoring is assumed to be < 100k rows, for which the
single-step ``ALTER COLUMN`` below is fast (seconds). Verify on
prod-shaped staging before deploy.

Downgrade restores ``tenant_id`` (NULL-able), backfills from
``Job.tenant_id`` via JOIN, recreates FK + index, and reverts
``job_id`` to nullable + ``ON DELETE SET NULL``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM external_service_calls WHERE job_id IS NULL")

    op.drop_constraint(
        "external_service_calls_job_id_fkey",
        "external_service_calls",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "external_service_calls_job_id_fkey",
        "external_service_calls",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="NO ACTION",
    )
    op.alter_column(
        "external_service_calls",
        "job_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

    op.drop_index(
        op.f("ix_external_service_calls_tenant_id"),
        table_name="external_service_calls",
    )
    op.drop_constraint(
        "external_service_calls_tenant_id_fkey",
        "external_service_calls",
        type_="foreignkey",
    )
    op.drop_column("external_service_calls", "tenant_id")


def downgrade() -> None:
    op.add_column(
        "external_service_calls",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
    )
    # Backfill tenant_id from Job before re-adding FK, so existing
    # rows satisfy the constraint.
    op.execute(
        """
        UPDATE external_service_calls esc
           SET tenant_id = j.tenant_id
          FROM jobs j
         WHERE esc.job_id = j.id
        """
    )
    op.create_foreign_key(
        "external_service_calls_tenant_id_fkey",
        "external_service_calls",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_external_service_calls_tenant_id"),
        "external_service_calls",
        ["tenant_id"],
        unique=False,
    )

    op.alter_column(
        "external_service_calls",
        "job_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.drop_constraint(
        "external_service_calls_job_id_fkey",
        "external_service_calls",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "external_service_calls_job_id_fkey",
        "external_service_calls",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )
