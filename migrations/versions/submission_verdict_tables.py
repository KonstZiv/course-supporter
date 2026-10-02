"""mentor-rebuild task 09b, commit K1: verdicts on criteria and the explanation of them.

Revision ID: submission_verdict_tables
Revises: task_criteria_archive_comment
Create Date: 2026-10-02

Creates two tables, both leaves of ``homework_submissions``:

1. ``submission_criterion_verdicts`` — one row per (submission, criterion) and
   per (submission, mandatory point): the machine verdict in full, with its
   evidence (``09b-verdicts/PRE-PLAN.md``, decisions 4, 7, 11, 12, 14; the
   columns are those of ``PRE-FLIGHT.md`` 9.8, ratified 2026-10-01). A verdict
   is addressed by the list it was judged against — ``criteria_layer`` +
   ``criteria_source_id`` — and ``item_id``. ``model_verdict`` beside
   ``verdict`` keeps what the model said apart from what stands (KD20). The
   database holds the rules a row must keep whoever writes it: one row per
   item of a submission, the shape of an identifier per kind, a weight on a
   criterion and none on a point, a quote under every "met" the model gave, a
   place only with a quote, the safeguard's source and its "met".
2. ``submission_explanations`` — the explanation stage's validated answer, one
   row per submission, kept for the builder of the review (``PRE-FLIGHT.md``
   9.7e): a path's checkpoint keeps no stage output.

Both carry ``tenant_id`` (every read is scoped by it, ``impl-rules#9``) and a
foreign key to the submission with ``ON DELETE CASCADE``; neither has
``deleted_at`` — as on ``student_feedback``, the readers filter on the live
submission. ``criteria_source_id`` and ``safeguard_submission_id`` carry NO
foreign key: the first addresses one of two tables by layer, the second is a
trace that must outlive a hard delete of the submission it names.

The vocabularies of the CHECK constraints are spelled out LITERALLY here, as in
``student_feedback_touch``: the ORM builds them from ``criteria_kinds``, and a
migration must not change meaning when the code later does. Column and table
comments are copied from the ORM; a test compares the live ones with it.

What is deliberately NOT here: no score and no "passed" — the code recounts
them from the rows; no index for the safeguard's lookup beyond the unique one —
it joins through the submission's own indexes, a handful of rows per student
and task.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). The ORM mirror is
``storage.orm.SubmissionCriterionVerdict`` and
``storage.orm.SubmissionExplanation``. New tables rewrite no row. The downgrade
drops them; lossless ONLY on a database where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "submission_verdict_tables"
down_revision: str | Sequence[str] | None = "task_criteria_archive_comment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERDICTS = "submission_criterion_verdicts"
_EXPLANATIONS = "submission_explanations"

# Spelled out, not derived from the enums: see the module docstring.
_LAYERS = "'author', 'model'"
_ITEM_KINDS = "'criterion', 'point'"
_WEIGHTS = "'must', 'should', 'may'"
_VERDICTS_VALUES = "'met', 'not_met'"
_CRITERION_ID = r"^c[1-9][0-9]*$"
_POINT_ID = r"^c[1-9][0-9]*\.p[1-9][0-9]*$"


def upgrade() -> None:
    """Create both tables with their CHECKs and unique indexes."""
    op.create_table(
        _VERDICTS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → Tenant. Every read and write of verdicts is scoped by it.",
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("homework_submissions.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → HomeworkSubmission whose review this verdict is part of.",
        ),
        sa.Column(
            "criteria_layer",
            sa.String(length=16),
            nullable=False,
            comment=(
                "Which list the verdict was judged against (CriteriaLayer): "
                "'author' — task_criteria_overrides, 'model' — task_criteria_lists."
            ),
        ),
        sa.Column(
            "criteria_source_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "Id of the list's row in the table criteria_layer names. NO "
                "foreign key: it addresses one of two tables, whose old rows stay "
                "as soft-deleted history."
            ),
        ),
        sa.Column(
            "item_id",
            sa.String(length=32),
            nullable=False,
            comment=(
                "The criterion ('c3') or the mandatory point ('c3.p2') judged, as "
                "identified within the list."
            ),
        ),
        sa.Column(
            "item_kind",
            sa.String(length=16),
            nullable=False,
            comment="What was judged (VerdictItemKind): 'criterion' or 'point'.",
        ),
        sa.Column(
            "weight",
            sa.String(length=16),
            nullable=True,
            comment=(
                "The criterion's weight category when judged (WeightCategory); "
                "NULL for a point, which has no weight of its own."
            ),
        ),
        sa.Column(
            "verdict",
            sa.String(length=16),
            nullable=False,
            comment="The verdict that stands (VerdictValue): 'met' or 'not_met'.",
        ),
        sa.Column(
            "model_verdict",
            sa.String(length=16),
            nullable=True,
            comment=(
                "What the model said (VerdictValue), kept beside the verdict that "
                "stands; NULL for a criterion derived from its points."
            ),
        ),
        sa.Column(
            "quote",
            sa.Text(),
            nullable=True,
            comment=(
                "The evidence: the short quote from the work that a 'met' stands "
                "on, or the model's quote that was not found in the work."
            ),
        ),
        sa.Column(
            "quote_file",
            sa.Text(),
            nullable=True,
            comment=(
                "The file the quote was found in: the archive member, or the "
                "submitted file's own name. NULL when there is no quote, or it "
                "was not found in the work."
            ),
        ),
        sa.Column(
            "quote_line_start",
            sa.Integer(),
            nullable=True,
            comment=(
                "First line of the quote in quote_file, from 1; NULL for a "
                "document (docx, pdf), whose lines are an artefact of extraction."
            ),
        ),
        sa.Column(
            "quote_line_end",
            sa.Integer(),
            nullable=True,
            comment="Last line of the quote in quote_file; NULL with quote_line_start.",
        ),
        sa.Column(
            "missing",
            sa.Text(),
            nullable=True,
            comment=(
                "One sentence on what is missing, for a 'not_met' the model gave; "
                "in the language of the course."
            ),
        ),
        sa.Column(
            "retried",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "The quote was not found at first and the model was asked once "
                "more for this item alone."
            ),
        ),
        sa.Column(
            "quote_not_found",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "The model said 'met' but its quote was not in the work even after "
                "the repeat — a flag for the author. The item reads 'not_met' "
                "unless the safeguard kept an earlier 'met'."
            ),
        ),
        sa.Column(
            "safeguard_fired",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "The resubmission safeguard kept an earlier 'met' of the same item "
                "of the same list, whose quote is still in the work."
            ),
        ),
        sa.Column(
            "safeguard_submission_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "The earlier submission whose verdict the safeguard kept. NO "
                "foreign key: a trace that outlives a hard delete of that "
                "submission."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the verdict was written.",
        ),
        sa.CheckConstraint(
            f"criteria_layer IN ({_LAYERS})",
            name="ck_submission_criterion_verdicts_criteria_layer",
        ),
        sa.CheckConstraint(
            f"item_kind IN ({_ITEM_KINDS})",
            name="ck_submission_criterion_verdicts_item_kind",
        ),
        sa.CheckConstraint(
            f"(item_kind <> 'criterion' OR item_id ~ '{_CRITERION_ID}') "
            f"AND (item_kind <> 'point' OR item_id ~ '{_POINT_ID}')",
            name="ck_submission_criterion_verdicts_item_id_shape",
        ),
        sa.CheckConstraint(
            f"(item_kind <> 'criterion' "
            f"OR (weight IS NOT NULL AND weight IN ({_WEIGHTS}))) "
            "AND (item_kind <> 'point' OR weight IS NULL)",
            name="ck_submission_criterion_verdicts_weight_of_criterion",
        ),
        sa.CheckConstraint(
            f"verdict IN ({_VERDICTS_VALUES})",
            name="ck_submission_criterion_verdicts_verdict",
        ),
        sa.CheckConstraint(
            f"model_verdict IS NULL OR model_verdict IN ({_VERDICTS_VALUES})",
            name="ck_submission_criterion_verdicts_model_verdict",
        ),
        sa.CheckConstraint(
            "item_kind = 'criterion' OR model_verdict IS NOT NULL",
            name="ck_submission_criterion_verdicts_point_judged_by_model",
        ),
        sa.CheckConstraint(
            "verdict <> 'met' OR model_verdict IS NULL OR quote IS NOT NULL",
            name="ck_submission_criterion_verdicts_met_has_quote",
        ),
        sa.CheckConstraint(
            "quote_file IS NULL OR quote IS NOT NULL",
            name="ck_submission_criterion_verdicts_place_of_a_quote",
        ),
        sa.CheckConstraint(
            "(quote_line_start IS NULL AND quote_line_end IS NULL) "
            "OR (quote_file IS NOT NULL AND quote_line_start IS NOT NULL "
            "AND quote_line_end IS NOT NULL AND quote_line_start >= 1 "
            "AND quote_line_end >= quote_line_start)",
            name="ck_submission_criterion_verdicts_lines",
        ),
        sa.CheckConstraint(
            "safeguard_fired = (safeguard_submission_id IS NOT NULL)",
            name="ck_submission_criterion_verdicts_safeguard_source",
        ),
        sa.CheckConstraint(
            "NOT safeguard_fired OR verdict = 'met'",
            name="ck_submission_criterion_verdicts_safeguard_keeps_met",
        ),
        sa.CheckConstraint(
            "NOT quote_not_found OR verdict = 'not_met' OR safeguard_fired",
            name="ck_submission_criterion_verdicts_unfound_quote_not_met",
        ),
        comment=(
            "Verdicts of a submission's review on criteria and mandatory "
            "points (mentor-rebuild task 09b). One row per (submission, item); "
            "the score and the pass are recounted from these rows by the code, "
            "never stored."
        ),
    )
    op.create_index(
        "ix_submission_criterion_verdicts_tenant_id", _VERDICTS, ["tenant_id"]
    )
    op.create_index(
        "uq_submission_criterion_verdicts_submission_item",
        _VERDICTS,
        ["submission_id", "item_id"],
        unique=True,
    )

    op.create_table(
        _EXPLANATIONS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK → Tenant. Every read and write of explanations is scoped by it."
            ),
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("homework_submissions.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → HomeworkSubmission the explanation is written for.",
        ),
        sa.Column(
            "language",
            sa.String(length=3),
            nullable=False,
            comment="ISO 639-3 code of the language the explanation is written in.",
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The stage's validated answer, as its own model dumps it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the explanation was first written.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment=(
                "When it was last replaced. On the ON CONFLICT DO UPDATE path this "
                "column is set EXPLICITLY: SQLAlchemy does not apply a Python-side "
                "onupdate to an upsert (dialects/postgresql/dml.py)."
            ),
        ),
        sa.CheckConstraint(
            "language ~ '^[a-z]{3}$'",
            name="ck_submission_explanations_language",
        ),
        comment=(
            "The explanation stage's answer for one submission (mentor-rebuild "
            "task 09b), kept for the builder of the review. One row per "
            "submission; a repeat run replaces it."
        ),
    )
    op.create_index(
        "ix_submission_explanations_tenant_id", _EXPLANATIONS, ["tenant_id"]
    )
    op.create_index(
        "uq_submission_explanations_submission_id",
        _EXPLANATIONS,
        ["submission_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop both tables (their indexes and constraints go with them)."""
    op.drop_table(_EXPLANATIONS)
    op.drop_table(_VERDICTS)
