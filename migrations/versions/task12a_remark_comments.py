"""mentor-rebuild task 12a, K1: comments on remarks and the state of a remark.

Revision ID: task12a_remark_comments
Revises: esc_request_traces
Create Date: 2026-11-16

After a review the student may answer a remark — a question, an objection, a
comment — and the next check answers every reply; between attempts every
remark has a state: new, open, fixed, broken. The product owner's decision of
2026-11-16: the replies and the Mentor's answers are a NEW table of comments
(not ``student_feedback``, whose "helped / not helped" touches stay as they
are), and the state, computed from the verdicts, is a column of the verdict
row. K1 is the schema alone: nothing writes or reads either place yet, so
production behaves as before.

1. ``remark_comments`` — one row per comment:
   ``tenant_id``; ``student_id`` + ``authored_document_id`` + ``task_version``
   — the student's work at one version of a task, as on
   ``homework_submissions``; ``submission_id`` — the attempt whose review the
   comment belongs to; ``item_id`` — the criterion or point whose remark it is
   on; ``parent_id`` — the comment it replies to (NULL starts a branch);
   ``author``, ``kind``, ``body``; ``topic``, ``topic_confidence``,
   ``hostile_category`` — the judgment of a student's comment; and
   ``answered_in_submission_id`` — the attempt whose review answered it.

   Foreign keys: tenant, student, submission and parent ``ON DELETE CASCADE``;
   the task ``RESTRICT``, as on ``homework_submissions``; the answering
   attempt ``ON DELETE SET NULL``.

   CHECKs: the three vocabularies (``author``, ``kind``, ``topic``); an
   ``answer`` by the mentor alone; a body that is not blank and not longer
   than 4000 characters; a confidence from 0 to 1; no topic judgment and no
   answering attempt on the mentor's comment; the item's identifier shaped as
   in ``submission_criterion_verdicts`` — a criterion or a point.

   Indexes: ``tenant_id``; ``(submission_id, item_id)`` — a review's comments,
   remark by remark; ``(student_id, authored_document_id, task_version)`` — a
   student's work at one version of a task; the same three columns, partial,
   over the student's comments not answered yet.

2. ``submission_criterion_verdicts.remark_state`` — ``VARCHAR(16)``, nullable:
   ``new`` | ``open`` | ``fixed`` | ``broken`` or NULL
   (``ck_submission_criterion_verdicts_remark_state``). A state says whether
   the item is credited now and was then — credited as the score counts it:
   ``met``, or ``not_met`` with ``quote_not_found``. That rule is the code's
   alone (``homework/criteria_verdicts.py``); the database holds the
   vocabulary, not the state's agreement with the row's verdict.

The vocabularies of the CHECK constraints are spelled out LITERALLY here, as in
``submission_verdict_tables``: the ORM builds them from ``remark_kinds`` and
``criteria_kinds``, and a migration must not change meaning when the code later
does. Column and table comments are copied from the ORM
(``storage.orm.RemarkComment``, ``storage.orm.SubmissionCriterionVerdict``); a
test compares the live ones with it.

No ``deleted_at`` and no soft-delete trigger, as on ``student_feedback`` and on
the verdicts: the rows go with a hard delete of their student or submission,
and the readers filter on the live ones.

Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).
A new table and a nullable column: no row is rewritten, and every verdict row
already there keeps NULL.

Downgrade: drops the column with its CHECK and the table — and with them
every state and every comment written since this revision; that loss is not
recoverable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "task12a_remark_comments"
down_revision: str | Sequence[str] | None = "esc_request_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENTS = "remark_comments"
_VERDICTS = "submission_criterion_verdicts"
_STATE_CHECK = "ck_submission_criterion_verdicts_remark_state"

# Spelled out, not derived from the enums: see the module docstring.
_AUTHORS = "'student', 'mentor'"
_KINDS = "'question', 'objection', 'comment', 'answer'"
_TOPICS = "'on_topic', 'off_topic', 'hostile'"
_STATES = "'new', 'open', 'fixed', 'broken'"
_CRITERION_ID = r"^c[1-9][0-9]*$"
_POINT_ID = r"^c[1-9][0-9]*\.p[1-9][0-9]*$"

_STATE_COMMENT = (
    "State of the item's remark between attempts (RemarkState), computed and "
    "written by task 12a: the item now against the same item in the previous "
    "completed attempt at the same task version. Credited — as the score "
    "counts it: 'met', or 'not_met' with quote_not_found (unconfirmed). "
    "'new' — not credited now, and there is no such attempt (the first one, "
    "another task version) or the item was not in it; 'open' — not credited "
    "then and now; 'fixed' — not credited then, credited now; 'broken' — "
    "credited then, not credited now. NULL — credited now, and credited then "
    "or nothing to compare with; also on every row written before task 12a "
    "computes it."
)


def upgrade() -> None:
    """Create ``remark_comments``; add ``remark_state`` with its CHECK."""
    op.create_table(
        _COMMENTS,
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            comment="Identifier of the comment.",
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="FK → Tenant. Every read and write of comments is scoped by it.",
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK → Student whose work the comment is on: the author of a "
                "student's comment, the student a Mentor's comment answers. With "
                "authored_document_id and task_version, the student's work at "
                "one version of a task. Goes with the student (ON DELETE "
                "CASCADE)."
            ),
        ),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            sa.ForeignKey("authored_documents.id", ondelete="RESTRICT"),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task), as on homework_submissions. "
                "RESTRICT, as there: comments are student history, not a "
                "derivative of the task."
            ),
        ),
        sa.Column(
            "task_version",
            sa.Text(),
            nullable=False,
            comment=(
                "Version of the task the branch lives in: the same value as "
                "homework_submissions.task_version of its submissions."
            ),
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("homework_submissions.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK → HomeworkSubmission whose review the comment belongs to: "
                "for a student's comment, the attempt whose review it replies "
                "to; for the Mentor's, the attempt in whose review it is given. "
                "Goes with that submission (ON DELETE CASCADE)."
            ),
        ),
        sa.Column(
            "item_id",
            sa.String(length=32),
            nullable=False,
            comment=(
                "The criterion ('c3') or the mandatory point ('c3.p2') whose "
                "remark the comment is on: the item_id of the submission's "
                "verdict on it."
            ),
        ),
        sa.Column(
            "parent_id",
            sa.Uuid(),
            sa.ForeignKey("remark_comments.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "FK → the comment this one replies to. NULL — the comment starts "
                "a branch. Goes with its parent (ON DELETE CASCADE)."
            ),
        ),
        sa.Column(
            "author",
            sa.String(length=16),
            nullable=False,
            comment="Who wrote it (CommentAuthor): 'student' or 'mentor'.",
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            comment=(
                "What it is (CommentKind): 'question', 'objection' or 'comment'; "
                "'answer' — the Mentor's alone."
            ),
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=False,
            comment=(
                "The text, as written: not empty, at most 4000 characters (the "
                "student's own, shorter limit is held by the code)."
            ),
        ),
        sa.Column(
            "topic",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Whether the student's comment keeps to its remark "
                "(CommentTopic): 'on_topic', 'off_topic' or 'hostile'; written "
                "by task 12a once judged. NULL — not judged yet, and always on "
                "the Mentor's comment."
            ),
        ),
        sa.Column(
            "topic_confidence",
            sa.Float(),
            nullable=True,
            comment=(
                "How sure the judgment of topic is, from 0 to 1; written by task "
                "12a with topic. NULL — not judged, and always on the Mentor's "
                "comment."
            ),
        ),
        sa.Column(
            "hostile_category",
            sa.String(length=32),
            nullable=True,
            comment=(
                "Short code of the category of a hostile attempt; written by "
                "task 12a, which sets its values (no list is held yet). NULL — "
                "no such attempt or not judged, and always on the Mentor's "
                "comment."
            ),
        ),
        sa.Column(
            "answered_in_submission_id",
            sa.Uuid(),
            sa.ForeignKey("homework_submissions.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "For a student's comment: FK → the attempt in whose review the "
                "Mentor answered it; written by task 12a with the answer. NULL — "
                "not answered yet, and always on the Mentor's comment; becomes "
                "NULL when that submission is deleted (ON DELETE SET NULL)."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When the comment was written.",
        ),
        sa.CheckConstraint(f"author IN ({_AUTHORS})", name="ck_remark_comments_author"),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_remark_comments_kind"),
        sa.CheckConstraint(
            "kind <> 'answer' OR author = 'mentor'",
            name="ck_remark_comments_answer_by_mentor",
        ),
        sa.CheckConstraint(
            "body ~ '[^[:space:]]'", name="ck_remark_comments_body_not_empty"
        ),
        sa.CheckConstraint(
            "char_length(body) <= 4000", name="ck_remark_comments_body_length"
        ),
        sa.CheckConstraint(
            f"topic IS NULL OR topic IN ({_TOPICS})", name="ck_remark_comments_topic"
        ),
        sa.CheckConstraint(
            "topic_confidence IS NULL OR (topic_confidence BETWEEN 0 AND 1)",
            name="ck_remark_comments_topic_confidence",
        ),
        sa.CheckConstraint(
            "author <> 'mentor' OR (topic IS NULL AND topic_confidence IS NULL "
            "AND hostile_category IS NULL)",
            name="ck_remark_comments_topic_of_student",
        ),
        sa.CheckConstraint(
            "author <> 'mentor' OR answered_in_submission_id IS NULL",
            name="ck_remark_comments_answered_of_student",
        ),
        sa.CheckConstraint(
            f"item_id ~ '{_CRITERION_ID}' OR item_id ~ '{_POINT_ID}'",
            name="ck_remark_comments_item_id_shape",
        ),
        comment=(
            "Comments on the remarks of a review (mentor-rebuild task 12a): the "
            "student's replies — a question, an objection, a comment — and the "
            "Mentor's answers to them. One row per comment; a row without a "
            "parent starts a branch. Written by task 12a."
        ),
    )
    op.create_index("ix_remark_comments_tenant_id", _COMMENTS, ["tenant_id"])
    op.create_index(
        "ix_remark_comments_submission_item", _COMMENTS, ["submission_id", "item_id"]
    )
    op.create_index(
        "ix_remark_comments_student_task_version",
        _COMMENTS,
        ["student_id", "authored_document_id", "task_version"],
    )
    op.create_index(
        "ix_remark_comments_unanswered",
        _COMMENTS,
        ["student_id", "authored_document_id", "task_version"],
        postgresql_where=sa.text(
            "author = 'student' AND answered_in_submission_id IS NULL"
        ),
    )

    op.add_column(
        _VERDICTS,
        sa.Column(
            "remark_state",
            sa.String(length=16),
            nullable=True,
            comment=_STATE_COMMENT,
        ),
    )
    op.create_check_constraint(
        _STATE_CHECK,
        _VERDICTS,
        f"remark_state IS NULL OR remark_state IN ({_STATES})",
    )


def downgrade() -> None:
    """Drop the column with its CHECK and the table — what they held is lost."""
    op.drop_constraint(_STATE_CHECK, _VERDICTS, type_="check")
    op.drop_column(_VERDICTS, "remark_state")
    op.drop_table(_COMMENTS)
