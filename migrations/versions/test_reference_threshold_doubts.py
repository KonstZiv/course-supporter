"""mentor-rebuild task 07: a test reference's pass mark, doubts and prompt trace.

Revision ID: test_reference_threshold_doubts
Revises: key_explanation_jobtype
Create Date: 2026-09-24

Task 07 turns a test's reference into what grades a submission, and three facts
it needs have no column yet. One revision, because the three arrive together
and each is read by the same new path; none of them touches ``jobs`` (the job
subject stays the task, ``TASK.md`` decision 9).

1. ``task_reference_overrides.pass_threshold`` — the author's pass mark,
   ``SMALLINT NULL``, in the units of the score: the percentage of questions
   answered right. NULL means "no pass mark": the review states the score and
   no verdict. ``ck_task_reference_overrides_pass_threshold`` keeps it within
   1-100; 0 would pass every submission and is not a threshold. It lives in the
   author's layer and outside the generation key, so changing it buys no
   generation.
2. ``task_references.doubts`` — ``JSONB NULL``, ``{question number: true}`` for
   the questions on which the model doubts the author's answer (prompt v2).
   ``{}`` means v2 found none; NULL means the version predates v2 and is read as
   "no doubt". A doubt hides the model's explanation from the student and never
   changes the score (``TASK.md`` invariant 2).
3. ``task_references.prompt_ref`` — ``TEXT NULL``, the prompt that wrote the
   explanations. Outside the version key on purpose (``TASK.md`` decision 3): a
   new prompt does not regenerate existing versions, so the only honest way to
   know which prompt wrote a version is to record it on the version. NULL for
   every version generated before task 07, all of them by v1.

Frozen SQL for the CHECK, no interpolation from application code (a migration
is an immutable snapshot). The ORM mirror is ``storage.orm.TaskReference`` and
``storage.orm.TaskReferenceOverride``.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). Adding nullable columns rewrites no row and fails none.
Downgrade drops the CHECK and the three columns; lossless ONLY on a database
where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "test_reference_threshold_doubts"
down_revision: str | Sequence[str] | None = "key_explanation_jobtype"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCES = "task_references"
_OVERRIDES = "task_reference_overrides"
_THRESHOLD_CHECK = "ck_task_reference_overrides_pass_threshold"


def upgrade() -> None:
    """Add the pass mark to the author's layer and two columns to the machine's."""
    op.add_column(
        _OVERRIDES,
        sa.Column(
            "pass_threshold",
            sa.SmallInteger(),
            nullable=True,
            comment=(
                "The author's pass mark, 1-100, in the units of the score: the "
                "percentage of questions answered right (task 07). NULL — no pass "
                "mark, so a review states the score and no verdict. Outside the "
                "generation key: changing it buys no generation. Replaced with the "
                "layer, so a replacement that omits it clears it."
            ),
        ),
    )
    op.create_check_constraint(
        _THRESHOLD_CHECK,
        _OVERRIDES,
        "pass_threshold IS NULL OR pass_threshold BETWEEN 1 AND 100",
    )
    op.add_column(
        _REFERENCES,
        sa.Column(
            "doubts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Questions on which the model doubts the author's answer, "
                "{question number: true} (task 07, prompt v2). {} — v2 found none; "
                "NULL — a version generated before v2, read as no doubt. A doubt "
                "hides the model's explanation from the student and never changes "
                "the score."
            ),
        ),
    )
    op.add_column(
        _REFERENCES,
        sa.Column(
            "prompt_ref",
            sa.Text(),
            nullable=True,
            comment=(
                "The prompt that wrote these explanations, as the ladder names it "
                "(task 07). Outside the version key on purpose: a new prompt does "
                "not regenerate existing versions. NULL — generated before task 07 "
                "(v1)."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the machine layer's two columns, then the pass mark and its CHECK."""
    op.drop_column(_REFERENCES, "prompt_ref")
    op.drop_column(_REFERENCES, "doubts")
    op.drop_constraint(_THRESHOLD_CHECK, _OVERRIDES, type_="check")
    op.drop_column(_OVERRIDES, "pass_threshold")
