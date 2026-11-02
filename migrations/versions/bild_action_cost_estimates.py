"""BIL-D: shadow price estimates of actions.

Revision ID: bild_action_cost_estimates
Revises: course_link_candidates
Create Date: 2026-11-02

Creates one table, ``action_cost_estimates``: the expected cost E of one action
(a review of a submission, the processing of a material job, the preparation
of a task), written before the action's first paid step, so that it can later
be compared with the action's actual cost in ``external_service_calls``.
Shadow mode: nothing reads it to charge or to show.

One row per action: the unique constraint on ``(action_kind, action_key)``
makes a second estimate of the same action a no-op (``ON CONFLICT DO
NOTHING``). The rows are a record that must outlive what they describe and
must never stand in the way of deleting it: the ids are plain indexed columns
WITHOUT foreign keys, and ``action_key`` keeps the action's identity as text.
Not ``ON DELETE SET NULL`` either: a cascading delete of an account reaches one
row by several such paths, and Postgres then fails the whole delete on the
foreign key of a row a sibling action already updated.

Column and table comments are copied from the ORM
(``storage.orm.ActionCostEstimate``). Hand-written; ``--autogenerate`` is not
used on this database (``DD-L4-A``). A new table rewrites no row. The
downgrade drops it; lossless ONLY on a database where nothing has written it
yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bild_action_cost_estimates"
down_revision: str | Sequence[str] | None = "course_link_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "action_cost_estimates"


def _id_column(column: str, comment: str) -> sa.Column:
    """A plain id column: no foreign key (module docstring)."""
    return sa.Column(column, sa.Uuid(), nullable=True, comment=comment)


def upgrade() -> None:
    """Create the table, its checks, its unique key and its lookup indexes."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "action_kind",
            sa.String(32),
            nullable=False,
            comment="review (one submission with all its jobs) | processing (one job) | task_preparation (one task version and kind of preparation).",
        ),
        sa.Column(
            "action_key",
            sa.String(200),
            nullable=False,
            comment="The action's identity as text: the submission id, the job id, or '<authored_document_id>:<version>:<kind>'. Kept when the rows it names are deleted.",
        ),
        _id_column(
            "tenant_id", "Tenant the action belongs to. No FK: survives the tenant."
        ),
        _id_column(
            "submission_id",
            "HomeworkSubmission of a review. No FK: survives the submission.",
        ),
        _id_column("job_id", "Job the estimate was made in. No FK: survives the job."),
        _id_column(
            "authored_document_id",
            "AuthoredDocument processed or prepared. No FK: survives the document.",
        ),
        sa.Column(
            "e_usd",
            sa.Numeric(18, 12),
            nullable=False,
            comment="Expected cost E in USD: the first rung of every step, at the catalog prices and the tariff window of the estimate's moment.",
        ),
        sa.Column(
            "p_units",
            sa.Integer(),
            nullable=True,
            comment="Price shown: ceil(E * (1 + margin) * units_per_usd). NULL while units_per_usd is not set.",
        ),
        sa.Column(
            "reserve_units",
            sa.Integer(),
            nullable=True,
            comment="Reserve: ceil(2 * E * units_per_usd). NULL while units_per_usd is not set.",
        ),
        sa.Column(
            "margin",
            sa.Numeric(8, 4),
            nullable=False,
            comment="The margin the price was computed with.",
        ),
        sa.Column(
            "units_per_usd",
            sa.Numeric(18, 6),
            nullable=True,
            comment="Units per 1 USD of cost (K) at the time; NULL = not set yet.",
        ),
        sa.Column(
            "catalog_version",
            sa.String(32),
            nullable=True,
            comment="Version of config/external_services.yaml the prices came from.",
        ),
        sa.Column(
            "breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The steps: step, model, tokens in/out (or billed seconds), prices, peak or not, cost.",
        ),
        sa.Column(
            "inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="What the estimate was computed from: sizes, durations, counts, the path, the moment.",
        ),
        sa.Column(
            "uncalibrated",
            sa.Boolean(),
            nullable=False,
            comment="True when a parameter used is still marked uncalibrated in config/cost_estimate.yaml, or units_per_usd is not set.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the estimate was written.",
        ),
        sa.CheckConstraint(
            "action_kind IN ('review', 'processing', 'task_preparation')",
            name="ck_action_cost_estimates_action_kind",
        ),
        sa.CheckConstraint(
            "(p_units IS NULL) = (units_per_usd IS NULL) "
            "AND (reserve_units IS NULL) = (units_per_usd IS NULL)",
            name="ck_action_cost_estimates_units_need_rate",
        ),
        sa.UniqueConstraint(
            "action_kind", "action_key", name="uq_action_cost_estimates_action"
        ),
        comment="Shadow price estimate of one action (BIL-D): expected cost E before the action runs, for calibration against the call register. One row per action; never deleted with what it is about.",
    )
    for column in ("tenant_id", "submission_id", "job_id", "authored_document_id"):
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column])


def downgrade() -> None:
    """Drop the table (its indexes and constraints go with it)."""
    op.drop_table(_TABLE)
