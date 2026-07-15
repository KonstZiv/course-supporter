"""L1b: typed job subject (subject_type/subject_id) + idempotency index.

Revision ID: l1b_job_subject
Revises: l1a_job_vocabulary
Create Date: 2026-07-15 00:00:00.000000

Job-lifecycle contract, step L1b (typed job subject). Replaces the untyped
JSONB archaeology of ``input_params`` with two typed columns that carry the
job's subject, and enforces "one subject — at most one in-flight job" in the
database.

Upgrade, in order (order is load-bearing):

1. Add ``subject_type`` (String(50)) + ``subject_id`` (Uuid), both nullable,
   no FK (polymorphic reference — orphans are legal by construction, KD12
   Path 3; R5).
2. Backfill from the untouched ``input_params`` per job_type. NULL-tolerant
   (R6): a row whose ``input_params`` lacks the identity key (early-format /
   unrecoverable historical rows on dev; ``s3_cleanup``, which has no natural
   subject) keeps BOTH columns NULL and the migration proceeds — fail-loud is
   wrong here, it would abort on legitimate legacy rows. The ``->> ... IS NOT
   NULL`` guard is why the ``::uuid`` cast only runs on rows that have the key.
3. ``ck_jobs_subject_pair`` — "both or neither" (subject_type/subject_id
   agree on NULL-ness). Added AFTER backfill so it validates the populated
   state.
4. ``ck_jobs_subject_type_legal`` — the legal ``job_type ↔ subject_type``
   pairs (SQL twin of ``jobs.job_type.JOB_SUBJECT_TYPE_PAIRS``; a test-lock
   asserts they agree), plus the ``subject_type IS NULL`` branch for
   s3_cleanup and unrecoverable historical rows. New job type → ALTER CHECK
   (a goal, not debt — the 3-bis L1a mirror).
5. ``uq_jobs_subject_in_flight`` — partial UNIQUE on (subject_type,
   subject_id) WHERE the job is in flight. Frozen SQL predicate (Alembic does
   not round-trip a partial WHERE — GIN 3.3b lesson — so this migration is
   hand-authored; the ORM ``__table_args__`` mirrors it for model honesty).
   NO ``NULLS NOT DISTINCT``: NULL subjects (s3_cleanup / historical) must NOT
   conflict — Postgres default NULL-distinct behaviour is load-bearing.

The status list in the index WHERE duplicates ``IN_FLIGHT_STATUSES``
(``storage.job_repository``); a derive-or-verify test-lock keeps the frozen
SQL and the code constant in step (contract R4).

The soft-delete trigger ``trg_jobs_block_update_when_deleted`` (task 0.1)
raises only on UPDATEs to soft-deleted rows; prod + dev carry zero soft-deleted
jobs (Job is in no soft-delete cascade — F6), so the backfill UPDATEs touch
only live rows and need no ``DISABLE TRIGGER``.

Downgrade is a full lossless round-trip: drop the index, both CHECKs, then both
columns. The columns are greenfield (no pre-existing data), and ``input_params``
is NEVER mutated by this migration, so a re-run of upgrade reconstructs the same
backfill byte-for-byte (idempotent round-trip both ways).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l1b_job_subject"
down_revision: str | Sequence[str] | None = "l1a_job_vocabulary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"
_PAIR_CHECK = "ck_jobs_subject_pair"
_TYPE_CHECK = "ck_jobs_subject_type_legal"
_INFLIGHT_INDEX = "uq_jobs_subject_in_flight"

# "both NULL or both set" — subject_type and subject_id agree on presence.
_PAIR_CONDITION = "(subject_type IS NULL) = (subject_id IS NULL)"

# Legal (job_type, subject_type) pairs + the NULL branch. SQL twin of
# jobs.job_type.JOB_SUBJECT_TYPE_PAIRS (test-lock asserts they agree).
_TYPE_CONDITION = (
    "(job_type = 'document_processing' AND subject_type = 'authored_document') "
    "OR (job_type = 'homework_processing' AND subject_type = 'homework_submission') "
    "OR (job_type = 'node_summary_regeneration' AND subject_type = 'course_node') "
    "OR (job_type = 'base_normalize' AND subject_type = 'project_base') "
    "OR subject_type IS NULL"
)

# In-flight predicate — duplicates IN_FLIGHT_STATUSES ({queued, active}); the
# derive-or-verify test-lock keeps them in step.
_INFLIGHT_WHERE = "status IN ('queued', 'active') AND deleted_at IS NULL"

# Per-job_type backfill statements — explicit frozen SQL, no interpolation
# (L1a discipline: a migration is an immutable snapshot). The
# ``->> key IS NOT NULL`` guard skips rows without the identity key
# (NULL-tolerant), so the ``::uuid`` cast only runs on rows that carry it.
# s3_cleanup is absent by design (no subject) — its rows stay NULL/NULL.
_BACKFILL_SQL: tuple[str, ...] = (
    "UPDATE jobs SET subject_type = 'authored_document', "
    "subject_id = (input_params->>'material_id')::uuid "
    "WHERE job_type = 'document_processing' "
    "AND input_params->>'material_id' IS NOT NULL",
    "UPDATE jobs SET subject_type = 'homework_submission', "
    "subject_id = (input_params->>'submission_id')::uuid "
    "WHERE job_type = 'homework_processing' "
    "AND input_params->>'submission_id' IS NOT NULL",
    "UPDATE jobs SET subject_type = 'course_node', "
    "subject_id = (input_params->>'vertex_node_id')::uuid "
    "WHERE job_type = 'node_summary_regeneration' "
    "AND input_params->>'vertex_node_id' IS NOT NULL",
    "UPDATE jobs SET subject_type = 'project_base', "
    "subject_id = (input_params->>'project_base_id')::uuid "
    "WHERE job_type = 'base_normalize' "
    "AND input_params->>'project_base_id' IS NOT NULL",
)


def upgrade() -> None:
    """Add subject columns, backfill, then the pair/type CHECKs and index."""
    op.add_column(
        _JOBS,
        sa.Column(
            "subject_type",
            sa.String(length=50),
            nullable=True,
            comment=(
                "L1b typed job subject kind (authored_document / "
                "homework_submission / course_node / project_base); NULL for "
                "s3_cleanup and unrecoverable historical rows."
            ),
        ),
    )
    op.add_column(
        _JOBS,
        sa.Column(
            "subject_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "L1b typed job subject id — polymorphic reference, no FK "
                "(orphans legal by construction, KD12 Path 3). Cancellation "
                "target + idempotency key."
            ),
        ),
    )

    for stmt in _BACKFILL_SQL:
        op.execute(stmt)

    op.create_check_constraint(_PAIR_CHECK, _JOBS, _PAIR_CONDITION)
    op.create_check_constraint(_TYPE_CHECK, _JOBS, _TYPE_CONDITION)
    op.create_index(
        _INFLIGHT_INDEX,
        _JOBS,
        ["subject_type", "subject_id"],
        unique=True,
        postgresql_where=sa.text(_INFLIGHT_WHERE),
    )


def downgrade() -> None:
    """Drop the index, both CHECKs, then both columns (lossless round-trip)."""
    op.drop_index(_INFLIGHT_INDEX, table_name=_JOBS)
    op.drop_constraint(_TYPE_CHECK, _JOBS, type_="check")
    op.drop_constraint(_PAIR_CHECK, _JOBS, type_="check")
    op.drop_column(_JOBS, "subject_id")
    op.drop_column(_JOBS, "subject_type")
