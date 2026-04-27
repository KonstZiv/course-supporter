"""0.3 task: Job model redesign — current_stage, stage_progress, drop estimated_at

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-27 18:00:00.000000

Phase-0 task 0.3 (vision §3 KD13). This migration covers the
column-level shape changes on ``jobs``:

* ``+current_stage VARCHAR(50) NULL`` — internal pipeline stage
  marker (``pass_1``/``pass_2a``/... per ``job_type``). Free-form;
  validation lives at the worker level per pipeline.
* ``+stage_progress JSONB NULL`` — per-job-type checkpoint state
  for reactivate-resume (KD4a in-flight resume + KD13). Schema is
  per-pipeline and intentionally not enforced at the DB level.
* ``-estimated_at`` — never set or read by any business logic;
  pure dead column carried from the initial schema.

Commit (b) of task 0.3 will extend the same revision (in a
follow-up edit on this file) with the
``materialnode_id → course_node_id`` column/constraint/index
rename. Splitting them across two commits keeps the git history
readable; the migration stays a single atomic step on the
``jobs`` table.

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
    """Add current_stage + stage_progress; drop estimated_at."""
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


def downgrade() -> None:
    """Restore estimated_at; drop current_stage + stage_progress."""
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
