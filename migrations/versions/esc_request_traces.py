"""Mentor: traces that make a cheaper evaluation comparable with today's.

Revision ID: esc_request_traces
Revises: biltestmark_test_batches
Create Date: 2026-11-16

The cost of a review is to come down — cheaper models on some stages, other
wording of the prompts, fewer repeated questions. The product owner
(2026-11-15, decided 2026-11-16) wants every review to leave in the database
what a comparison "before / after" needs. Five gaps were found; four of them
are columns.

Adds to ``external_service_calls`` (shared with billing: columns are only
ADDED, all nullable; no existing column, index or constraint changes):

* ``request_kind`` — ``VARCHAR(16)``: which request of a stage an attempt
  is — ``first``, ``item_repeat`` (the evaluation's ``ask_again``),
  ``structural_retry`` (after the validator refused), ``infra_retry`` (after
  a timeout, 429 or 5xx). ``ck_esc_request_kind`` — one of the four or NULL.
* ``seed`` — ``INTEGER``: the seed the request carried to the provider.
* ``temperature`` — ``DOUBLE PRECISION``: the temperature it carried.
* ``rung_index`` — ``SMALLINT``: the rung's position on the stage's ladder,
  from 1. ``ck_esc_rung_index`` — NULL or at least 1.

Adds to ``submission_criterion_verdicts``:

* ``first_model_verdict`` — ``VARCHAR(16)``: the model's first word on a
  retried item. ``ck_submission_criterion_verdicts_first_model_verdict`` — a
  verdict value or NULL, and a value only on a retried row (not the converse:
  a row retried before this revision has none).
* ``answered_by_call_id`` — ``UUID``, a foreign key to
  ``external_service_calls.id`` with ``ON DELETE SET NULL``
  (``fk_submission_criterion_verdicts_answered_by_call_id``): the call whose
  answer the verdict stands on. The register outlives a submission; a
  verdict row goes with its submission, so the key points one way only. No
  index: the register's rows are never deleted, and reads go from a verdict
  to its call by the call's primary key.

And corrects the comment of ``submission_criterion_verdicts.quote_from_base``,
which still said nothing wrote or read it (the project's chain K3 writes it,
the explanation reads it).

The vocabularies are spelled out LITERALLY here, as in
``verdict_repeat_reason``: the ORM builds the CHECKs from
``call_outcome.RequestKind`` and ``criteria_kinds.VerdictValue``, and a
migration must not change meaning when the code later does. Every comment is
copied from the ORM (``storage.orm.ExternalServiceCall``,
``SubmissionCriterionVerdict``); a test compares the live ones with it. No
existing row is rewritten: rows written before this revision keep NULL.

Hand-written; ``--autogenerate`` is not used on this database (``DD-L4-A``).

Downgrade: drops the key, the CHECKs and the six columns, and puts the old
comment back — every trace written since this revision is lost, which is not
recoverable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "esc_request_traces"
down_revision: str | Sequence[str] | None = "biltestmark_test_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ESC = "external_service_calls"
_VERDICTS = "submission_criterion_verdicts"
_FK = "fk_submission_criterion_verdicts_answered_by_call_id"
_CK_KIND = "ck_esc_request_kind"
_CK_RUNG = "ck_esc_rung_index"
_CK_FIRST = "ck_submission_criterion_verdicts_first_model_verdict"

# Spelled out, not derived from the enums: see the module docstring.
_KINDS = "'first', 'item_repeat', 'structural_retry', 'infra_retry'"
_VERDICT_VALUES = "'met', 'not_met'"

_REQUEST_KIND_COMMENT = (
    "Which request of the stage this attempt is (RequestKind): first / "
    "item_repeat / structural_retry / infra_retry. Written by the ladder router "
    "on attempt rows. NULL on trace rows, on rows of no model call and on rows "
    "written before revision esc_request_traces."
)
_SEED_COMMENT = (
    "The seed the request was sent with, as the provider got it. NULL = no seed "
    "was sent, the row is not an attempt of the ladder router, or it was "
    "written before revision esc_request_traces."
)
_TEMPERATURE_COMMENT = (
    "The temperature the request was sent with. NULL = none was set, the row is "
    "not an attempt of the ladder router, or it was written before revision "
    "esc_request_traces."
)
_RUNG_INDEX_COMMENT = (
    "Position of the row's rung on the stage's ladder, from 1; an item repeat "
    "keeps the position of the rung it is sent to. On attempt and trace rows of "
    "the ladder router. NULL = outside a ladder, or written before revision "
    "esc_request_traces."
)
_FIRST_MODEL_VERDICT_COMMENT = (
    "What the model said about the item in its first answer (VerdictValue), on "
    "a retried row alone; model_verdict is its word after the repeat. NULL when "
    "the item was not asked again, and on rows written before revision "
    "esc_request_traces."
)
_ANSWERED_BY_CALL_ID_COMMENT = (
    "FK → ExternalServiceCall: the attempt whose answer the verdict stands on — "
    "of a retried item, the one homework/criteria_verdicts.py::_word_to_settle "
    "chose. NULL for a criterion derived from its points, when the call's row "
    "was not written, and on rows written before revision esc_request_traces."
)
_QUOTE_FROM_BASE_COMMENT = (
    "The item fell for a quote of the project's base, the author's code, not "
    "the student's own (project chain K3): 'not_met', never unconfirmed. "
    "Written from homework/criteria_verdicts.py by "
    "storage/submission_verdict_repository.py; read by the explanation stage. "
    "Always false outside the project path."
)
# What task13_attempt_ledger wrote; put back by the downgrade.
_QUOTE_FROM_BASE_OLD_COMMENT = (
    "The quote stands on the project's base, not on the student's own work "
    "(task 13, reserved for the project path). Nothing writes or reads it yet."
)


def upgrade() -> None:
    """Add the columns, their CHECKs and the key; existing rows keep NULL."""
    op.add_column(
        _ESC,
        sa.Column(
            "request_kind",
            sa.String(length=16),
            nullable=True,
            comment=_REQUEST_KIND_COMMENT,
        ),
    )
    op.add_column(
        _ESC, sa.Column("seed", sa.Integer(), nullable=True, comment=_SEED_COMMENT)
    )
    op.add_column(
        _ESC,
        sa.Column(
            "temperature", sa.Float(), nullable=True, comment=_TEMPERATURE_COMMENT
        ),
    )
    op.add_column(
        _ESC,
        sa.Column(
            "rung_index", sa.SmallInteger(), nullable=True, comment=_RUNG_INDEX_COMMENT
        ),
    )
    op.create_check_constraint(
        _CK_KIND, _ESC, f"request_kind IS NULL OR request_kind IN ({_KINDS})"
    )
    op.create_check_constraint(
        _CK_RUNG, _ESC, "rung_index IS NULL OR rung_index >= 1"
    )

    op.add_column(
        _VERDICTS,
        sa.Column(
            "first_model_verdict",
            sa.String(length=16),
            nullable=True,
            comment=_FIRST_MODEL_VERDICT_COMMENT,
        ),
    )
    op.add_column(
        _VERDICTS,
        sa.Column(
            "answered_by_call_id",
            sa.Uuid(),
            nullable=True,
            comment=_ANSWERED_BY_CALL_ID_COMMENT,
        ),
    )
    op.create_check_constraint(
        _CK_FIRST,
        _VERDICTS,
        "(first_model_verdict IS NULL OR first_model_verdict IN "
        f"({_VERDICT_VALUES})) AND (first_model_verdict IS NULL OR retried)",
    )
    op.create_foreign_key(
        _FK,
        _VERDICTS,
        _ESC,
        ["answered_by_call_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        _VERDICTS,
        "quote_from_base",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        existing_server_default=sa.text("false"),
        comment=_QUOTE_FROM_BASE_COMMENT,
        existing_comment=_QUOTE_FROM_BASE_OLD_COMMENT,
    )


def downgrade() -> None:
    """Drop what the upgrade added — the traces written are lost."""
    op.alter_column(
        _VERDICTS,
        "quote_from_base",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        existing_server_default=sa.text("false"),
        comment=_QUOTE_FROM_BASE_OLD_COMMENT,
        existing_comment=_QUOTE_FROM_BASE_COMMENT,
    )
    op.drop_constraint(_FK, _VERDICTS, type_="foreignkey")
    op.drop_constraint(_CK_FIRST, _VERDICTS, type_="check")
    op.drop_column(_VERDICTS, "answered_by_call_id")
    op.drop_column(_VERDICTS, "first_model_verdict")

    op.drop_constraint(_CK_RUNG, _ESC, type_="check")
    op.drop_constraint(_CK_KIND, _ESC, type_="check")
    op.drop_column(_ESC, "rung_index")
    op.drop_column(_ESC, "temperature")
    op.drop_column(_ESC, "seed")
    op.drop_column(_ESC, "request_kind")
