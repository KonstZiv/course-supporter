"""L1a: canonicalise Job.job_type + add job_type/status CHECK constraints.

Revision ID: l1a_job_vocabulary
Revises: codemat_schema_columns
Create Date: 2026-07-14 00:00:00.000000

Job-lifecycle contract, step L1a (vocabulary + status ownership).

Upgrade, in order (order is load-bearing — the CHECK validates every
existing row on ADD, so the legacy values must be gone first):

1. Data-migrate the two legacy ``job_type`` tokens to their canonical
   KD13 names: ``ingest → document_processing``, ``homework →
   homework_processing``. The other three types (``node_summary_regeneration``,
   ``s3_cleanup``, ``base_normalize``) already store canonical values.
2. ``ck_jobs_job_type`` — the five canonical KD13 job types. Deferred from
   task 0.3 ("deferred to Phase 2.x"); this IS that step. Plain-SQL ``IN (...)`` per
   the ``ck_homework_delivery_mode`` precedent (NOT a native PG enum).
3. ``ck_jobs_status`` — the five canonical statuses. The dead ``running`` /
   ``completed`` tokens never reached prod or dev (measured 2026-07-14), so
   the CHECK kills nothing.

The soft-delete trigger ``trg_jobs_block_update_when_deleted`` (task 0.1)
raises only on UPDATEs to soft-deleted rows; prod + dev carry zero
soft-deleted jobs (Job is in no soft-delete cascade), so the data UPDATE
touches only live rows and needs no ``DISABLE TRIGGER``.

Downgrade is a full round-trip: drop both CHECKs, then reverse the rename.
The reverse is lossless because prod carried zero pre-existing
``document_processing`` / ``homework_processing`` rows before this migration
(measured), so every such row originated from the forward rename — and on a
rollback the reverted code emits/reads the legacy tokens, so mapping ALL
``document_processing → ingest`` is the correct restore, not a corruption.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "l1a_job_vocabulary"
down_revision: str | Sequence[str] | None = "codemat_schema_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_JOB_TYPE_CHECK = "ck_jobs_job_type"
_STATUS_CHECK = "ck_jobs_status"

_JOB_TYPE_VALUES = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize'"
)
_STATUS_VALUES = "'queued', 'active', 'complete', 'failed', 'cancelled'"


def upgrade() -> None:
    """Rename legacy job_type values, then add the two vocabulary CHECKs."""
    op.execute(
        "UPDATE jobs SET job_type = 'document_processing' WHERE job_type = 'ingest'"
    )
    op.execute(
        "UPDATE jobs SET job_type = 'homework_processing' WHERE job_type = 'homework'"
    )
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES})"
    )
    op.create_check_constraint(_STATUS_CHECK, _JOBS, f"status IN ({_STATUS_VALUES})")


def downgrade() -> None:
    """Drop the CHECKs, then restore the legacy job_type lexicon."""
    op.drop_constraint(_STATUS_CHECK, _JOBS, type_="check")
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.execute(
        "UPDATE jobs SET job_type = 'ingest' WHERE job_type = 'document_processing'"
    )
    op.execute(
        "UPDATE jobs SET job_type = 'homework' WHERE job_type = 'homework_processing'"
    )
