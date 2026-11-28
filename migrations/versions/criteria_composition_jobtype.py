"""DD-SP-DR: admit the job that composes a task's criteria list on the author's request.

Revision ID: criteria_composition_jobtype
Revises: v1_author_interface_language
Create Date: 2026-11-27

Until now the criteria list of a task was composed only inside the review of
the first student's work, under that review's ``homework_processing`` job. The
author may now ask for it before any submission
(``POST /documents/{id}/criteria/compose``), and that work needs a ``Job`` of
its own: the call register's ``job_id`` is mandatory (KD5), the payer's fact is
summed from the register rows of one job, and the in-flight index
``uq_jobs_subject_in_flight`` is what makes it one composition per task.

Two CHECKs widen together, by the pattern of ``key_explanation_jobtype`` (a
Postgres CHECK cannot be altered in place, so each is a drop + re-add with the
widened set):

1. ``ck_jobs_job_type`` — the value set gains ``criteria_composition``.
2. ``ck_jobs_subject_type_legal`` — the legal-pair set gains
   ``(criteria_composition, authored_document)``.

The subject is the task, as for ``document_processing`` and
``key_explanation``: a second composition of a task whose processing — or
another preparation — is in flight is refused by the database.

Frozen SQL, no interpolation from application code. The ORM mirror
(``storage.orm.Job.__table_args__``) and ``jobs.job_type`` carry the same sets;
``test_l1b_invariants`` asserts the code, the ORM and THIS migration's frozen
SQL agree — this migration is now the authority for
``ck_jobs_subject_type_legal`` (it supersedes ``key_explanation_jobtype``).

Hand-written (``DD-L4-A``); ``--autogenerate`` is not used on this database.
Widening validates every existing row against a SUPERSET, so no row can fail
the re-add. Downgrade restores the seven-type / six-pair sets and is lossless
ONLY on a database carrying no ``criteria_composition`` rows: with one, the
re-added CHECK refuses it and the downgrade fails whole (Postgres DDL is
transactional), the same condition the key-explanation widening carries.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "criteria_composition_jobtype"
down_revision: str | Sequence[str] | None = "v1_author_interface_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_JOB_TYPE_CHECK = "ck_jobs_job_type"
_SUBJECT_TYPE_CHECK = "ck_jobs_subject_type_legal"

# Frozen value sets — explicit, no interpolation from application code.
_JOB_TYPE_VALUES_NEW = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize', "
    "'document_preparation', 'key_explanation', 'criteria_composition'"
)
_JOB_TYPE_VALUES_OLD = (
    "'document_processing', 'node_summary_regeneration', "
    "'homework_processing', 's3_cleanup', 'base_normalize', "
    "'document_preparation', 'key_explanation'"
)

# Legal (job_type, subject_type) pairs + the NULL branch. SQL twin of
# jobs.job_type.JOB_SUBJECT_TYPE_PAIRS (test-lock asserts they agree). The NEW
# set adds the criteria_composition ↔ authored_document pair; the OLD set is the
# exact key_explanation_jobtype condition restored on downgrade.
_SUBJECT_TYPE_CONDITION_NEW = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'document_preparation' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR (job_type = 'key_explanation' AND subject_type = 'authored_document') "
    "OR (job_type = 'criteria_composition' AND subject_type = 'authored_document') "
    "OR subject_type IS NULL"
)
_SUBJECT_TYPE_CONDITION_OLD = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'document_preparation' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR (job_type = 'key_explanation' AND subject_type = 'authored_document') "
    "OR subject_type IS NULL"
)


def upgrade() -> None:
    """Widen both jobs vocabulary CHECKs with ``criteria_composition``."""
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_NEW})"
    )
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_NEW)


def downgrade() -> None:
    """Restore the seven-type / six-pair sets (lossless iff no composition rows)."""
    op.drop_constraint(_SUBJECT_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(_SUBJECT_TYPE_CHECK, _JOBS, _SUBJECT_TYPE_CONDITION_OLD)
    op.drop_constraint(_JOB_TYPE_CHECK, _JOBS, type_="check")
    op.create_check_constraint(
        _JOB_TYPE_CHECK, _JOBS, f"job_type IN ({_JOB_TYPE_VALUES_OLD})"
    )
