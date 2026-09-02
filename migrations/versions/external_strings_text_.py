"""Store externally-authored strings as TEXT, not width-bounded VARCHAR.

Revision ID: external_strings_text
Revises: esc_reasoning_tokens
Create Date: 2026-09-02

A column width is a promise about a value the system controls. For a value it
does NOT control, the width is a trap: too small and the write raises
``StringDataRightTruncation``, which surfaces as a 500 on an upload that is
perfectly ordinary from the student's side. That is what happened on
2026-09-02 — the first ``.docx`` submission after the gates pass, because the
browser sends
``application/vnd.openxmlformats-officedocument.wordprocessingml.document``,
71 characters, into a ``VARCHAR(50)``.

Three groups, all of them values written by somebody other than us:

* ``homework_submissions.file_type`` (50) — the MIME type the CLIENT declares.
  The longest OOXML type is 73 characters; there is no useful upper bound to
  pick, and picking one only postpones the same failure.
* ``external_service_calls.prompt_ref`` (50) — the longest prompt reference in
  the ladder configs is ALREADY 51
  (``prompts/mentor_layered_evaluation_node_course/v1.md``). It has never
  overflowed only because nothing writes the column yet (DD-CQ-C); fixing that
  debt without this migration would have started failing every ESC row for
  that stage.
* ``document_segments.description`` / ``document_summaries.description`` (512)
  — written by a MODEL. The "≤512" is an instruction in the prompt and a
  ``max_length`` on some of the draft schemas, not on all of them; where the
  schema does not carry it, a 513-character description would fail the same
  way, on authored ingestion rather than on a submission.

The bound does not disappear — it moves to where it can answer properly. A
value that is too long should be rejected by validation, with a reason the
caller can act on, not truncated or turned into a 500 by the storage layer.

Downgrade restores the previous widths. It will fail on rows that have since
grown past them, which is correct: silently truncating a student's MIME type
or a model's description to make a rollback fit would lose data.

DELIBERATELY NOT included: the whole-schema comment and index drift that
``alembic revision --autogenerate`` reports on this database (some 200
operations, DD-L4-A). That drift is orthogonal, pre-existing and separately
ratified; folding it into a hotfix would make the hotfix unreviewable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "external_strings_text"
down_revision: str | Sequence[str] | None = "esc_reasoning_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEGMENT_DESC_COMMENT = (
    "1-2 sentence segment description (Pass 2a LLM emits per segment; "
    "populated for text/web at C7, NULL for media pre-Pass-2c). NOT in "
    "content_hash formula -- DD-2.1-W defers to Phase 1.3/1.4."
)
_SUMMARY_DESC_COMMENT_NEW = (
    "Optional brief description; ≤512 is a MODEL instruction (vision §2.2), "
    "enforced where the draft schema carries max_length -- not by the column, "
    "which would truncate-or-500 instead of validating"
)
_SUMMARY_DESC_COMMENT_OLD = "Optional brief description (≤512 per vision §2.2)"
_FILE_TYPE_COMMENT_NEW = (
    "MIME type as declared by the client; not width-bounded -- the docx type "
    "alone is 71 characters"
)
_FILE_TYPE_COMMENT_OLD = "MIME type of the uploaded file"


def upgrade() -> None:
    op.alter_column(
        "homework_submissions",
        "file_type",
        existing_type=sa.VARCHAR(length=50),
        type_=sa.Text(),
        comment=_FILE_TYPE_COMMENT_NEW,
        existing_comment=_FILE_TYPE_COMMENT_OLD,
        existing_nullable=False,
    )
    op.alter_column(
        "external_service_calls",
        "prompt_ref",
        existing_type=sa.VARCHAR(length=50),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "document_segments",
        "description",
        existing_type=sa.VARCHAR(length=512),
        type_=sa.Text(),
        existing_comment=_SEGMENT_DESC_COMMENT,
        existing_nullable=True,
    )
    op.alter_column(
        "document_summaries",
        "description",
        existing_type=sa.VARCHAR(length=512),
        type_=sa.Text(),
        comment=_SUMMARY_DESC_COMMENT_NEW,
        existing_comment=_SUMMARY_DESC_COMMENT_OLD,
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "document_summaries",
        "description",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=512),
        comment=_SUMMARY_DESC_COMMENT_OLD,
        existing_comment=_SUMMARY_DESC_COMMENT_NEW,
        existing_nullable=True,
    )
    op.alter_column(
        "document_segments",
        "description",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=512),
        existing_comment=_SEGMENT_DESC_COMMENT,
        existing_nullable=True,
    )
    op.alter_column(
        "external_service_calls",
        "prompt_ref",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=50),
        existing_nullable=True,
    )
    op.alter_column(
        "homework_submissions",
        "file_type",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=50),
        comment=_FILE_TYPE_COMMENT_OLD,
        existing_comment=_FILE_TYPE_COMMENT_NEW,
        existing_nullable=False,
    )
