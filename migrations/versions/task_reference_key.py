"""mentor-rebuild task 06: what a task is checked against.

Revision ID: task_reference_key
Revises: student_feedback_touch
Create Date: 2026-09-19

The two tables a reference lives in, and nothing else. ``task_references`` is
the machine layer: append-only versions of a generated reference (today the
explanations of a test's answer key). ``task_reference_overrides`` is the
author's layer: one row per (task, kind), replaced whole. The shape is the
ratified one (``06-reference/TASK.md``, the section of decisions taken from the
probe report):

* **Two tables, not one with two groups of columns.** The machine layer
  accumulates versions because each generation is a new fact; the author's
  answers are one current truth with no versions of their own. In one table
  half the columns would be empty by construction in half the rows — exactly
  what ``NodeSummaryFinal`` avoids by being a table of its own.
* ``uq_task_reference_axes_active`` — UNIQUE over (task, kind,
  source_content_hash, source_task_type, answers_hash, language) WHERE
  ``state <> 'failed'``. This is what makes "the same triple costs zero model
  calls" a rule of the DATABASE and not only of the code (TASK.md invariant 4).
  ``failed`` is excluded so a version that gave up cannot block its retry. The
  partial WHERE is hand-written here because Alembic does not round-trip a
  partial index (the GIN 3.3b lesson), and the ORM carries the model-honesty
  twin.
* **No ``deleted_at`` and no soft-delete trigger on either table**, like
  ``project_bases`` and for the same reason: versions are append-only and go
  away with the task through ``ON DELETE CASCADE``. Both are therefore LEAVES
  of the content_hash graph and enter no content_hash formula.
* ``answers <> '{}'::jsonb`` is a CHECK rather than route validation alone: an
  empty key is not a key, and without the constraint the only thing between "the
  author sent nothing" and a stored row that makes a test look answerable would
  be a branch in Python.
* The two vocabularies are ``CHECK`` constraints, and their values are spelled
  out LITERALLY here on purpose: the ORM builds the same lists from
  ``ReferenceKind`` / ``ReferenceState``, and a migration must not change
  meaning when the code later does. Widening one is a new migration that
  re-issues the CHECK — Postgres has no "widen a CHECK". Task 08 adds
  ``'mandatory_points'`` to ``kind`` exactly that way.

**What is deliberately NOT here: the ``jobs`` vocabulary.** These versions are
produced by a queued job, so the work also needs a ``JobType`` member and the
two ``jobs`` CHECKs widened for it. Those arrive in a SECOND revision, together
with the ARQ task itself, and not here — because ``test_seam_completeness``
locks an EQUALITY between the registered Job-bearing tasks and the ``JobType``
members. A slice that admits the type before the task exists cannot be green,
so the type and the work that bears it travel in one commit (decision of
2026-09-19 after the A1 gate went red).

Hand-written (SPRINT rule 6 of §2). ``alembic revision --autogenerate`` is NOT
used and its output is NOT included: on this database it reports ~200 operations
of pre-existing whole-schema comment and index drift (``DD-L4-A``), of which the
ones belonging to this revision are the two tables below and nothing else.

Forward-only in spirit: the tables are brand new, so no back-fill is possible.
The downgrade drops them, which is lossless only in the sense every table drop
is — on a database that has reference rows, it discards them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "task_reference_key"
down_revision: str | Sequence[str] | None = "student_feedback_touch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCES = "task_references"
_OVERRIDES = "task_reference_overrides"

# Spelled out, not derived from the enums: see the module docstring.
_KINDS = "'test_key'"
_STATES = "'pending', 'ready', 'failed'"


def _create_reference_versions() -> None:
    """The machine layer: append-only generated versions."""
    op.create_table(
        _REFERENCES,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            sa.ForeignKey("authored_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task). CASCADE — deleting the task "
                "removes its reference versions. The composite "
                "(authored_document_id, version) unique index covers this FK "
                "(leftmost prefix), so no standalone FK index."
            ),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment=(
                "Monotonic 1-based version per document. A new generation is a "
                "new version; an existing one is never rewritten. Unique per "
                "document."
            ),
        ),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Which kind of reference this version carries (ReferenceKind). "
                "Today: 'test_key'. Task 08 adds 'mandatory_points' as a "
                "MEMBER — a widened CHECK, not a new table."
            ),
        ),
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Content-axis version key = AuthoredDocument.content_hash at "
                "generation time (mirrors TaskCriteria.source_content_hash)."
            ),
        ),
        sa.Column(
            "source_task_type",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Type-axis version key = AuthoredDocument.task_type at "
                "generation time. Re-typing the task invalidates this version."
            ),
        ),
        sa.Column(
            "answers_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Answer-axis version key: SHA-256 over the author's answers in "
                "canonical form (sorted question numbers, sorted labels within "
                "each). Canonical because JSONB key order is not guaranteed "
                "and an unstable digest would buy a second paid generation for "
                "an unchanged key."
            ),
        ),
        sa.Column(
            "language",
            sa.String(length=10),
            nullable=False,
            comment=(
                "Language the explanations are written in, ISO 639-3 — the "
                "course language in the first echelon. Part of the version "
                "key: the same key in another language is another generation."
            ),
        ),
        sa.Column(
            "explanations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Generated explanations, {question number: text} — one per "
                "question, why the right answer is right. NULL until READY."
            ),
        ),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
            comment=(
                "Generation lifecycle (ReferenceState): pending → ready | "
                "failed(reason). Enforced by ck_task_references_state."
            ),
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment=(
                "Human-readable reason when state='failed' (the ladder or "
                "funds-port refusal). NULL otherwise."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When this version was created (pending).",
        ),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_task_references_kind"),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_task_references_state"),
        comment=(
            "Append-only generated reference versions for a task "
            "(mentor-rebuild task 06) — today the explanations of a test's "
            "answer key. One live version per (task, kind, content, type, "
            "answers, language)."
        ),
    )
    op.create_index(
        "uq_task_reference_document_version",
        _REFERENCES,
        ["authored_document_id", "version"],
        unique=True,
    )
    # The zero-calls rule, held by the database (TASK.md invariant 4). Partial
    # WHERE written out here: Alembic does not round-trip a partial index, so
    # this migration — not autogenerate — is its authority.
    op.create_index(
        "uq_task_reference_axes_active",
        _REFERENCES,
        [
            "authored_document_id",
            "kind",
            "source_content_hash",
            "source_task_type",
            "answers_hash",
            "language",
        ],
        unique=True,
        postgresql_where=sa.text("state <> 'failed'"),
    )
    op.create_index(
        "ix_task_references_document_kind",
        _REFERENCES,
        ["authored_document_id", "kind"],
    )


def _create_author_overrides() -> None:
    """The author's layer: one row per (task, kind), replaced whole."""
    op.create_table(
        _OVERRIDES,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            sa.ForeignKey("authored_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task). CASCADE — deleting the task "
                "removes the author's layer with it. The composite "
                "(authored_document_id, kind) unique index covers this FK "
                "(leftmost prefix), so no standalone FK index."
            ),
        ),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Which kind of reference this layer belongs to "
                "(ReferenceKind), the same vocabulary as task_references.kind."
            ),
        ),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment=(
                "The author's answers, {question number: [option labels]}. A "
                "set of labels, not one label: how a multi-label question "
                "scores is task 07's business, but the shape must not need a "
                "migration then."
            ),
        ),
        sa.Column(
            "author_explanations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "The author's own explanations for individual questions, "
                "{question number: text}. Outside the generation key on "
                "purpose (ratified 2026-09-19): editing one must not buy a "
                "fresh generation of the whole set."
            ),
        ),
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "AuthoredDocument.content_hash of the task text these answers "
                "were written against. Compared with the live one to decide "
                "whether the answers carry over to a new version or the task "
                "waits for a key."
            ),
        ),
        sa.Column(
            "carried_over",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment=(
                "True when these answers reached the current task version "
                "automatically (the question numbers were unchanged) rather "
                "than being sent for it. Cleared when the author replaces the "
                "layer."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the author first sent a key for this task.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment=(
                "When the layer last changed. On the ON CONFLICT DO UPDATE "
                "path this column is set EXPLICITLY: SQLAlchemy does not apply "
                "a Python-side onupdate to an upsert "
                "(dialects/postgresql/dml.py)."
            ),
        ),
        sa.CheckConstraint(
            f"kind IN ({_KINDS})", name="ck_task_reference_overrides_kind"
        ),
        sa.CheckConstraint(
            "answers <> '{}'::jsonb",
            name="ck_task_reference_overrides_answers_present",
        ),
        comment=(
            "The author's layer of a task reference (mentor-rebuild task 06) — "
            "one row per (task, kind), replaced whole. Its absence is the "
            "'waiting for a key' state."
        ),
    )
    op.create_index(
        "uq_task_reference_override_document_kind",
        _OVERRIDES,
        ["authored_document_id", "kind"],
        unique=True,
    )


def upgrade() -> None:
    """Create both reference tables — the machine layer and the author's."""
    _create_reference_versions()
    _create_author_overrides()


def downgrade() -> None:
    """Drop both tables in reverse order (indexes and constraints go with them)."""
    op.drop_table(_OVERRIDES)
    op.drop_table(_REFERENCES)
