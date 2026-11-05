"""mentor-rebuild task 13, K2: the attempt ledger.

Revision ID: task13_attempt_ledger
Revises: bila_billing_accounts
Create Date: 2026-11-05

Adds to ``homework_submissions`` the columns the attempt ledger writes where the
doors first read the work (``homework/attempt_ledger.py``):

* ``attempt_no`` — the student's earlier attempts at the task + 1;
* ``previous_submission_id`` — FK to the latest earlier attempt, ``ON DELETE SET
  NULL``, with its own index: a hard delete of a submission looks up the rows
  pointing at it, and the table has no other index that starts with the column;
* ``task_version`` — the version of the task the work was read against;
* ``fingerprint`` — the K1 fingerprint of the work (SHA-256 hex);
* ``attempt_receipt`` — the receipt of the attempt (JSONB);

and the partial index ``ix_homework_submissions_attempt_fingerprint`` on
``(student_id, authored_document_id, fingerprint) WHERE fingerprint IS NOT
NULL`` — the attempts of a student at a task by their work.

Adds to ``submission_criterion_verdicts`` ``quote_from_base`` (boolean, NOT
NULL, ``server_default false``), reserved for the project path; nothing writes
or reads it yet.

Every new ``homework_submissions`` column is nullable and NOT back-filled: a row
from before K2 keeps NULL, which the ledger reads as "no record". The one
``NOT NULL`` column takes its server default on the existing rows.

Column comments are copied from the ORM (``storage.orm.HomeworkSubmission``,
``storage.orm.SubmissionCriterionVerdict``). Hand-written; ``--autogenerate`` is
not used on this database (``DD-L4-A``). The downgrade drops what the upgrade
added — lossless only while nothing has written the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "task13_attempt_ledger"
down_revision: str | None = "bila_billing_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBMISSIONS = "homework_submissions"
_VERDICTS = "submission_criterion_verdicts"
_PREVIOUS_FK = "homework_submissions_previous_submission_id_fkey"
_PREVIOUS_INDEX = "ix_homework_submissions_previous_submission_id"
_FINGERPRINT_INDEX = "ix_homework_submissions_attempt_fingerprint"


def upgrade() -> None:
    op.add_column(
        _SUBMISSIONS,
        sa.Column(
            "attempt_no",
            sa.Integer(),
            nullable=True,
            comment="Attempt number (task 13): the student's earlier attempts at "
            "this task + 1, counted when the doors first read the work. An "
            "attempt is a live submission that got a review (completed / "
            "delivered); a refused, off-task or failed one takes no number from "
            "the next. NULL before K2 and when the doors refused.",
        ),
    )
    op.add_column(
        _SUBMISSIONS,
        sa.Column(
            "previous_submission_id",
            sa.Uuid(),
            nullable=True,
            comment="FK → the student's latest earlier attempt at this task (task "
            "13), NULL for the first. SET NULL — a hard delete of that attempt "
            "only clears the pointer.",
        ),
    )
    op.create_foreign_key(
        _PREVIOUS_FK,
        _SUBMISSIONS,
        _SUBMISSIONS,
        ["previous_submission_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(_PREVIOUS_INDEX, _SUBMISSIONS, ["previous_submission_id"])
    op.add_column(
        _SUBMISSIONS,
        sa.Column(
            "task_version",
            sa.Text(),
            nullable=True,
            comment="Version of the task this submission was read against (task "
            "13): AuthoredDocument.content_hash for a text or project task, the "
            "TestVersion id the stored answers name for a test. NULL before K2.",
        ),
    )
    op.add_column(
        _SUBMISSIONS,
        sa.Column(
            "fingerprint",
            sa.Text(),
            nullable=True,
            comment="Fingerprint of the work (task 13, K1 AttemptSnapshotter): "
            "SHA-256 hex, equal exactly when the work is the same. NULL before K2.",
        ),
    )
    op.add_column(
        _SUBMISSIONS,
        sa.Column(
            "attempt_receipt",
            postgresql.JSONB(),
            nullable=True,
            comment="Receipt of the attempt (task 13): {attempt_no, "
            "previous_submission_id, task_version, task_version_changed, "
            "fingerprint}. NULL before K2.",
        ),
    )
    op.create_index(
        _FINGERPRINT_INDEX,
        _SUBMISSIONS,
        ["student_id", "authored_document_id", "fingerprint"],
        postgresql_where=sa.text("fingerprint IS NOT NULL"),
    )

    op.add_column(
        _VERDICTS,
        sa.Column(
            "quote_from_base",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="The quote stands on the project's base, not on the student's "
            "own work (task 13, reserved for the project path). Nothing writes or "
            "reads it yet.",
        ),
    )


def downgrade() -> None:
    op.drop_column(_VERDICTS, "quote_from_base")

    op.drop_index(_FINGERPRINT_INDEX, table_name=_SUBMISSIONS)
    op.drop_column(_SUBMISSIONS, "attempt_receipt")
    op.drop_column(_SUBMISSIONS, "fingerprint")
    op.drop_column(_SUBMISSIONS, "task_version")
    op.drop_index(_PREVIOUS_INDEX, table_name=_SUBMISSIONS)
    op.drop_constraint(_PREVIOUS_FK, _SUBMISSIONS, type_="foreignkey")
    op.drop_column(_SUBMISSIONS, "previous_submission_id")
    op.drop_column(_SUBMISSIONS, "attempt_no")
