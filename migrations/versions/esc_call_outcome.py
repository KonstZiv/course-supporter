"""mentor-rebuild task 01: the call register says what happened, not only whether.

Revision ID: esc_call_outcome
Revises: external_strings_text
Create Date: 2026-09-14

``external_service_calls.success`` has only ever meant "the transport returned
a response". Everything else — an empty body at the output ceiling, a body the
validator refused, a rung skipped or abandoned — was readable only by parsing
``error_message`` (DD-SP-AC). This migration gives each of those its own
queryable column, in one step:

* ``outcome`` — the row's result (``CallOutcome``, nine values), CHECK-bound.
* ``finish_reason`` — the connector-normalised finish reason (four values),
  CHECK-bound.
* ``skip_reason`` — why a rung was skipped without a call (three values),
  CHECK-bound. Task 02 widens it with "rate limited" by its own migration.
* ``prompt_hash`` / ``input_hash`` — which template version and which exact
  input an attempt used (reproducibility).
* ``input_text`` / ``output_text`` — the full input (setting-gated, refused in
  production) and the response body (per-stage opt-in).
* ``authenticity`` / ``completeness`` — per-review metrics, written on their
  own row with no provider, model or transport.

``provider``, ``model_id`` and ``success`` become NULLABLE: a ladder trace row
(skipped / abandoned rung) and the metrics row involve no call, and a
placeholder value there would lie — a skip marked ``success = true`` reads as
a call in every register query. On a NULL ``success`` with no
``error_message`` the canonical failure measure ``NOT success OR
error_message IS NOT NULL`` evaluates to NULL, so such rows stay out of the
failure count (pinned by ``test_external_service_call_db.py``).

NO back-fill. Every new column is NULL on historical rows: an ``outcome``
reconstructed from ``success`` + ``error_message`` would be a guess, forever
indistinguishable from a recorded fact.

The CHECK value lists are spelled out literally on purpose: the ORM builds the
same lists from the enums, and a migration must not change meaning when the
code later does. ``tests/integration/test_external_service_call_db.py`` writes
every enum member through the database, so a drift fails loudly.

DELIBERATELY NOT included: the whole-schema comment and index drift that
``alembic revision --autogenerate`` reports on this database (DD-L4-A). This
file is hand-written (SPRINT rule 6) and carries only the columns above.

Downgrade drops the new columns and restores NOT NULL on provider, model_id
and success. It fails on rows written with NULL there (trace and metrics rows),
which is correct: silently inventing a provider or a transport result to make
a rollback fit would fabricate register history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "esc_call_outcome"
down_revision: str | Sequence[str] | None = "external_strings_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "external_service_calls"

_OUTCOMES = (
    "success",
    "transport_error",
    "provider_refusal",
    "input_overflow",
    "empty_at_output_ceiling",
    "empty",
    "invalid_content",
    "skipped",
    "abandoned",
)
_FINISH_REASONS = ("output_ceiling", "stop", "other", "unknown")
_SKIP_REASONS = (
    "provider_not_configured",
    "provider_disabled",
    "input_budget_exceeded",
)


def _nullable_in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IS NULL OR {column} IN ({listed})"


def upgrade() -> None:
    """Add the outcome / trace / reproducibility / metrics columns."""
    op.add_column(
        _TABLE,
        sa.Column(
            "prompt_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 of the prompt template's model-facing sections at "
            "render time. prompt_ref names the file; the file is edited in place, "
            "so only this hash says which version was sent.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "input_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 of the canonical input of this one attempt: rendered "
            "prompts, attachment digests and the output-affecting parameters. Same "
            "input, same hash — the reproducibility key.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "input_text",
            sa.Text(),
            nullable=True,
            comment="Full rendered input. Written only when the full-input setting "
            "is raised, which production refuses to boot with: it carries the "
            "student's submission.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "output_text",
            sa.Text(),
            nullable=True,
            comment="Response body as returned. Written only on stages that enable "
            "output recording in their ladder config.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "outcome",
            sa.String(length=32),
            nullable=True,
            comment="What happened to this row (CallOutcome): the result of a call, "
            "or a skipped / abandoned ladder rung. NULL on rows written before the "
            "column existed and on the per-review metrics row.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "finish_reason",
            sa.String(length=16),
            nullable=True,
            comment="Why generation stopped, normalised by the connector "
            "(FinishReason): output_ceiling / stop / other / unknown.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "skip_reason",
            sa.String(length=32),
            nullable=True,
            comment="Why a ladder rung was skipped without a call (SkipReason). Set "
            "only on outcome = 'skipped'.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "authenticity",
            sa.Float(),
            nullable=True,
            comment="Per-review metric: share of claims supported by a reference. "
            "NULL on call rows and when the review has no claims.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "completeness",
            sa.Float(),
            nullable=True,
            comment="Per-review metric: share of claims covered by a verdict. NULL "
            "on call rows and when the review has no claims.",
        ),
    )

    op.create_check_constraint(
        "ck_esc_outcome", _TABLE, _nullable_in("outcome", _OUTCOMES)
    )
    op.create_check_constraint(
        "ck_esc_finish_reason", _TABLE, _nullable_in("finish_reason", _FINISH_REASONS)
    )
    op.create_check_constraint(
        "ck_esc_skip_reason", _TABLE, _nullable_in("skip_reason", _SKIP_REASONS)
    )

    op.alter_column(
        _TABLE,
        "provider",
        existing_type=sa.String(length=50),
        nullable=True,
        comment="Provider that was called. NULL = no call was made (the "
        "per-review metrics row).",
    )
    op.alter_column(
        _TABLE,
        "model_id",
        existing_type=sa.String(length=100),
        nullable=True,
        comment="Model that was called. NULL = no call was made (the "
        "per-review metrics row).",
    )
    op.alter_column(
        _TABLE,
        "success",
        existing_type=sa.Boolean(),
        nullable=True,
        comment="Transport result only: did the call return a response. NULL = "
        "no call was made (ladder trace and metrics rows). Whether the response "
        "was usable is outcome.",
    )


def downgrade() -> None:
    """Drop the new columns and restore NOT NULL (fails on NULL rows, by design)."""
    op.alter_column(
        _TABLE,
        "success",
        existing_type=sa.Boolean(),
        nullable=False,
        comment=None,
    )
    op.alter_column(
        _TABLE,
        "model_id",
        existing_type=sa.String(length=100),
        nullable=False,
        comment=None,
    )
    op.alter_column(
        _TABLE,
        "provider",
        existing_type=sa.String(length=50),
        nullable=False,
        comment=None,
    )

    op.drop_constraint("ck_esc_skip_reason", _TABLE, type_="check")
    op.drop_constraint("ck_esc_finish_reason", _TABLE, type_="check")
    op.drop_constraint("ck_esc_outcome", _TABLE, type_="check")

    for column in (
        "completeness",
        "authenticity",
        "skip_reason",
        "finish_reason",
        "outcome",
        "output_text",
        "input_text",
        "input_hash",
        "prompt_hash",
    ):
        op.drop_column(_TABLE, column)
