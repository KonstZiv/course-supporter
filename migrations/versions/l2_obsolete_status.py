"""L2: add ``obsolete`` terminal status to ck_jobs_status.

Revision ID: l2_obsolete_status
Revises: l1b_job_subject
Create Date: 2026-07-16 00:00:00.000000

Job-lifecycle contract, step L2 (execution seam), commit 1. Adds the sixth
canonical ``Job.status`` token — ``obsolete`` — the seam's terminal for
"subject vanished" (the subject was soft-deleted before/while the worker
reached the job; skip-if-dead). Ратифіковано 2026-07-16 (Рат.1): a NEW token,
not a reuse of ``cancelled`` (which means "terminated externally / by a human")
— the two causes stay distinguishable at the token.

A Postgres CHECK cannot be altered in place, so this is a drop + re-add of
``ck_jobs_status`` with the widened value set. Frozen SQL, no interpolation
(L1a/L1b discipline: a migration is an immutable snapshot). The ORM mirror
(``storage.orm.Job.__table_args__``) and the code state machine
(``JOB_TRANSITIONS`` / ``_STATUS_CATEGORY`` in ``storage.job_repository``) carry
the same set; a test-lock asserts the three agree (contract R4).

``uq_jobs_subject_in_flight`` is NOT touched: ``obsolete`` is ``at_rest`` (no
worker holds it), so it is absent from the index's in-flight WHERE predicate
(``status IN ('queued', 'active')``) by construction — the idempotency index is
unaffected.

Downgrade re-adds the five-value CHECK. It is lossless ONLY on a database that
carries no ``obsolete`` rows (the ADD CONSTRAINT would otherwise fail its
validation scan) — consistent with the forward-only spirit of a status-widening
migration; on a clean dev round-trip (no obsolete rows yet) it is byte-for-byte
reversible.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "l2_obsolete_status"
down_revision: str | Sequence[str] | None = "l1b_job_subject"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_STATUS_CHECK = "ck_jobs_status"

# Frozen value sets — explicit, no interpolation from application code.
_STATUS_VALUES_L2 = "'queued', 'active', 'complete', 'failed', 'cancelled', 'obsolete'"
_STATUS_VALUES_L1A = "'queued', 'active', 'complete', 'failed', 'cancelled'"


def upgrade() -> None:
    """Drop the five-value status CHECK, re-add it with ``obsolete``."""
    op.drop_constraint(_STATUS_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_STATUS_CHECK, _JOBS, f"status IN ({_STATUS_VALUES_L2})")


def downgrade() -> None:
    """Restore the five-value status CHECK (lossless iff no obsolete rows)."""
    op.drop_constraint(_STATUS_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _STATUS_CHECK, _JOBS, f"status IN ({_STATUS_VALUES_L1A})"
    )
