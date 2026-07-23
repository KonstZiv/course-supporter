"""№21 BE1+BE3: add ``document_preparation`` to the two jobs vocabulary CHECKs.

Revision ID: n21_prep_jobtype
Revises: n21_authored_file_roles
Create Date: 2026-07-22 00:00:01.000000

№21 "typicality is not a property of the file", combined commit BE1+BE3. Adds
the sixth canonical ``Job.job_type`` — ``document_preparation`` (deterministic
CODE-archive prep: extract + typicality + tree → file-role proposal; zero LLM),
whose subject is the same ``authored_document`` as ``document_processing``.

Two CHECKs widen together (a Postgres CHECK cannot be altered in place, so each
is a drop + re-add with the widened set):

1. ``ck_jobs_job_type`` — the value set gains ``document_preparation``.
2. ``ck_jobs_subject_type_legal`` — the legal-pair set gains
   ``(document_preparation, authored_document)``.

Frozen SQL, no interpolation from application code (L1a/L1b/L2 discipline: a
migration is an immutable snapshot). The ORM mirror
(``storage.orm.Job.__table_args__``) and ``jobs.job_type`` carry the same sets;
a test-lock (``test_l1b_invariants``) asserts the code, the ORM, and THIS
migration's frozen SQL agree — this migration is now the authority for
``ck_jobs_subject_type_legal`` (it supersedes the l1b definition), while l1b
stays the authority for the ``uq_jobs_subject_in_flight`` index it also froze.

The prod-constraint audit (2026-07-22) confirmed both CHECKs already exist on
prod with the five-type / four-pair sets — hence a real ALTER, not a first
creation. Widening validates every existing row against a SUPERSET, so no row
can fail the re-add.

Downgrade restores the five-type / four-pair sets. Lossless ONLY on a database
carrying no ``document_preparation`` rows (the narrowed ADD CONSTRAINT would
otherwise fail its validation scan) — the forward-only spirit of a vocabulary
widening; byte-for-byte reversible on a clean round-trip (no prep rows yet).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "n21_prep_jobtype"
down_revision: str | Sequence[str] | None = "n21_authored_file_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_JOB_TYPE_CHECK = "ck_jobs_job_type"
_SUBJECT_TYPE_CHECK = "ck_jobs_subject_type_legal"

# Frozen value sets — explicit, no interpolation from application code.
_JOB_TYPE_VALUES_NEW = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize', "
    "'document_preparation'"
)
_JOB_TYPE_VALUES_OLD = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize'"
)

# Legal (job_type, subject_type) pairs + the NULL branch. SQL twin of
# jobs.job_type.JOB_SUBJECT_TYPE_PAIRS (test-lock asserts they agree). The NEW
# set adds the document_preparation ↔ authored_document pair; the OLD set is the
# exact l1b condition restored on downgrade.
_SUBJECT_TYPE_CONDITION_NEW = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'document_preparation' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR subject_type IS NULL"
)
_SUBJECT_TYPE_CONDITION_OLD = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR subject_type IS NULL"
)


def upgrade() -> None:
    """Widen both jobs vocabulary CHECKs with ``document_preparation``."""
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_NEW})"
    )
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_NEW)


def downgrade() -> None:
    """Restore the five-type / four-pair sets (lossless iff no prep rows)."""
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_OLD)
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_OLD})"
    )
