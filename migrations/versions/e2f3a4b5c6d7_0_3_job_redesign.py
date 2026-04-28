"""0.3 task: Job model redesign — current_stage, stage_progress, drop estimated_at, rename FK

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-27 18:00:00.000000

Phase-0 task 0.3 (vision §3 KD13). This migration covers all
schema-level changes on ``jobs`` for the redesign:

* ``+current_stage VARCHAR(50) NULL`` — internal pipeline stage
  marker (``pass_1``/``pass_2a``/... per ``job_type``). Free-form;
  validation lives at the worker level per pipeline.
* ``+stage_progress JSONB NULL`` — per-job-type checkpoint state
  for reactivate-resume (KD4a in-flight resume + KD13). Schema is
  per-pipeline and intentionally not enforced at the DB level.
* ``-estimated_at`` — never set or read by any business logic;
  pure dead column carried from the initial schema.
* ``materialnode_id → course_node_id`` rename — column, FK
  constraint (``jobs_materialnode_id_fkey`` →
  ``jobs_course_node_id_fkey``), and index
  (``ix_jobs_materialnode_id`` → ``ix_jobs_course_node_id``).
  PostgreSQL handles the column rename atomically with zero data
  movement; the constraint and index renames are independent
  catalog updates. Names verified against the prod DB before
  scripting.

Commits (a) and (b) of task 0.3 stay separate in the git history,
but share this single migration file — the column-level changes
land first, then the rename block is appended. Repository state
between the two commits is partial-but-valid; the migration is
always a single atomic step on the ``jobs`` table when applied.

NOT in 0.3 (deferred, see POST-MR-NOTES):

* Drop ``depends_on`` — coupled to Phase 2.x rewrite of
  ``methodist_orchestrator`` and ``generation_orchestrator``
  from Job-graph to single-Job-with-stages pattern. Column
  carries an updated DEPRECATED comment in the ORM; column
  itself stays.
* DB CHECK constraint on ``job_type`` — current call-sites emit
  legacy values (``ingest``/``generate_structure``/...) outside
  KD13's 4-value enum. Application-level validation via
  ``JobType`` enum lands later in 0.3 (commit c); the DB CHECK
  follows in Phase 2.x along with call-site migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add current_stage + stage_progress; drop estimated_at; rename FK column."""
    op.add_column(
        "jobs",
        sa.Column(
            "current_stage",
            sa.String(length=50),
            nullable=True,
            comment=(
                "Internal pipeline stage marker per vision §3 KD13. "
                "Free-form per job_type (e.g. pass_1/pass_2a/pass_2b/pass_2c "
                "for document_processing; bottomup/topdown for "
                "node_summary_regeneration; safety/sanity/review/delivery for "
                "homework_processing; unused for s3_cleanup). Validation lives "
                "at the worker level per pipeline."
            ),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "stage_progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Per-job-type checkpoint state for resume after retry "
                "(KD4a in-flight resume + KD13 reactivate). Schema is "
                "per-pipeline and intentionally not enforced at the DB level."
            ),
        ),
    )
    op.drop_column("jobs", "estimated_at")

    # Rename Job.materialnode_id → Job.course_node_id (KD13).
    # Column rename is atomic; FK constraint and index names need
    # separate ALTER calls (Postgres does not auto-rename them when
    # the underlying column changes).
    op.execute("ALTER TABLE jobs RENAME COLUMN materialnode_id TO course_node_id")
    op.execute(
        "ALTER TABLE jobs RENAME CONSTRAINT jobs_materialnode_id_fkey "
        "TO jobs_course_node_id_fkey"
    )
    op.execute("ALTER INDEX ix_jobs_materialnode_id RENAME TO ix_jobs_course_node_id")


def downgrade() -> None:
    """Reverse FK rename; restore estimated_at; drop new columns."""
    op.execute("ALTER INDEX ix_jobs_course_node_id RENAME TO ix_jobs_materialnode_id")
    op.execute(
        "ALTER TABLE jobs RENAME CONSTRAINT jobs_course_node_id_fkey "
        "TO jobs_materialnode_id_fkey"
    )
    op.execute("ALTER TABLE jobs RENAME COLUMN course_node_id TO materialnode_id")

    op.add_column(
        "jobs",
        sa.Column(
            "estimated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.drop_column("jobs", "stage_progress")
    op.drop_column("jobs", "current_stage")
