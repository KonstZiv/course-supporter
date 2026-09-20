"""mentor-rebuild task 06: admit the job that writes a key's explanations.

Revision ID: key_explanation_jobtype
Revises: task_reference_key
Create Date: 2026-09-19

The second half of task 06's schema change, and a revision of its own for one
measured reason: ``test_seam_completeness`` locks an EQUALITY between the
registered Job-bearing ARQ tasks and the ``JobType`` members, so a slice that
admits the type before the task exists cannot be green. The type and the work
that bears it therefore travel in one commit — this revision — while the tables
they fill arrived in ``task_reference_key``.

Two CHECKs widen together, by the pattern of ``n21_prep_jobtype`` (a Postgres
CHECK cannot be altered in place, so each is a drop + re-add with the widened
set):

1. ``ck_jobs_job_type`` — the value set gains ``key_explanation``.
2. ``ck_jobs_subject_type_legal`` — the legal-pair set gains
   ``(key_explanation, authored_document)``.

The subject is the TASK, not the reference version it produces. The version
does not exist when the job is enqueued — the service creates it and then asks
for the work — and ``uq_jobs_subject_in_flight`` keys idempotency on the
subject, so a second request for the same task while one is in flight is
refused by the database rather than by a branch.

Frozen SQL, no interpolation from application code (L1a/L1b/L2 discipline: a
migration is an immutable snapshot). The ORM mirror
(``storage.orm.Job.__table_args__``) and ``jobs.job_type`` carry the same sets;
a test-lock (``test_l1b_invariants``) asserts the code, the ORM, and THIS
migration's frozen SQL agree — this migration is now the authority for
``ck_jobs_subject_type_legal`` (it supersedes ``n21_prep_jobtype``), while l1b
stays the authority for the ``uq_jobs_subject_in_flight`` index it froze.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). Widening validates every existing row against a
SUPERSET, so no row can fail the re-add. Downgrade restores the six-type /
six-pair sets; lossless ONLY on a database carrying no ``key_explanation``
rows, the same condition the prep-type widening carries.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "key_explanation_jobtype"
down_revision: str | Sequence[str] | None = "task_reference_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_JOB_TYPE_CHECK = "ck_jobs_job_type"
_SUBJECT_TYPE_CHECK = "ck_jobs_subject_type_legal"

# Frozen value sets — explicit, no interpolation from application code.
_JOB_TYPE_VALUES_NEW = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize', "
    "'document_preparation', 'key_explanation'"
)
_JOB_TYPE_VALUES_OLD = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize', "
    "'document_preparation'"
)

# Legal (job_type, subject_type) pairs + the NULL branch. SQL twin of
# jobs.job_type.JOB_SUBJECT_TYPE_PAIRS (test-lock asserts they agree). The NEW
# set adds the key_explanation ↔ authored_document pair; the OLD set is the
# exact n21_prep_jobtype condition restored on downgrade.
_SUBJECT_TYPE_CONDITION_NEW = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'document_preparation' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR (job_type = 'key_explanation' AND subject_type = 'authored_document') "
    "OR subject_type IS NULL"
)
_SUBJECT_TYPE_CONDITION_OLD = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'document_preparation' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR subject_type IS NULL"
)


def upgrade() -> None:
    """Widen both jobs vocabulary CHECKs with ``key_explanation``."""
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_NEW})"
    )
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_NEW)


def downgrade() -> None:
    """Restore the six-type / six-pair sets (lossless iff no key rows)."""
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_OLD)
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_OLD})"
    )
