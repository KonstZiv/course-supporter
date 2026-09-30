"""mentor-rebuild task 08, commit K1: the criteria list of a task version and its author's edit.

Revision ID: criteria_list_tables
Revises: test_object_tables
Create Date: 2026-09-29

What a task is checked against becomes a list with a lifecycle (task 08,
section 9, decision 2) and an author's layer beside it (section 3.6). One
revision, because both tables arrive together and are read by the same new code:

1. ``task_criteria_lists`` — the machine layer. A row is the criteria list of
   one task version (content hash + task type). Its ``state`` (pending → ready |
   failed) is what lets several workers share one composition: the first
   submission inserts a ``pending`` claim and commits it, calls the model
   holding no lock and no connection, and marks the row ``ready``; a second
   submission that finds the claim waits and takes the ready list.
   ``uq_task_criteria_lists_authored_document_id_active`` keeps ONE live row — a
   claim or a ready list — per task; earlier versions are soft-deleted history.
   ``claimed_at`` is what an abandoned claim is taken over by: a conditional
   update compares it. Three CHECKs hold the lifecycle: the state vocabulary; a
   ready row is complete; a failed row is history (soft-deleted), so a failure
   never blocks the next claim.
2. ``task_criteria_overrides`` — the author's edit of a list: replaced whole,
   bound to the task version it was written for and never carried to a new one;
   a replacement and a reset soft-delete the previous edit, so every earlier
   state stays as a snapshot. One live edit per task
   (``uq_task_criteria_overrides_authored_document_id_active``); an empty list
   is not an edit (``ck_task_criteria_overrides_criteria_present``).
3. The comment of ``task_references.kind`` loses the plan to add
   ``'mandatory_points'`` there: task 08 keeps mandatory items inside the
   criteria list (section 3.3), so the old comment would lie.

What is deliberately NOT here: ``task_criteria`` is untouched — it stays as an
archive and in the soft-delete cascade, and no row is copied from it; no
``BEFORE UPDATE`` soft-delete trigger, as on every soft-deletable table added
after task 0.1 (the partial indexes and the repositories are the guards).

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). The ORM mirror is ``storage.orm.TaskCriteriaList``,
``storage.orm.TaskCriteriaOverride`` and the comment of ``TaskReference.kind``.
New tables rewrite no row. The downgrade drops them and puts the old comment
back; lossless ONLY on a database where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "criteria_list_tables"
down_revision: str | Sequence[str] | None = "test_object_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LISTS = "task_criteria_lists"
_OVERRIDES = "task_criteria_overrides"
_REFERENCES = "task_references"

_SOFT_DELETE_COMMENT = (
    "Soft-delete timestamp (vision KD3). NULL = active; NOT NULL = soft-deleted."
)

_KIND_BEFORE = (
    "Which kind of reference this version carries (ReferenceKind). "
    "Today: 'test_key'. Task 08 adds 'mandatory_points' as a MEMBER — a "
    "widened CHECK, not a new table."
)
_KIND_AFTER = (
    "Which kind of reference this version carries (ReferenceKind). "
    "Today: 'test_key'. A new kind is a new member and a widened CHECK."
)


def upgrade() -> None:
    """Create the criteria list and the author's edit; narrow one comment."""
    op.create_table(
        _LISTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task). Hard-delete cascades with the "
                "document; soft-delete cascades via "
                "AuthoredDocument.__cascades_soft_delete_to__. One live row per task "
                "(uq_task_criteria_lists_authored_document_id_active)."
            ),
        ),
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Content-axis version key = AuthoredDocument.content_hash when "
                "the composition was claimed (the TaskCriteria axis, same meaning)."
            ),
        ),
        sa.Column(
            "source_task_type",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Type-axis version key = AuthoredDocument.task_type when the "
                "composition was claimed; re-typing the task makes the list stale."
            ),
        ),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment=(
                "Composition lifecycle (CriteriaListState): pending (claimed, "
                "the model is being called) → ready | failed(reason). Enforced by "
                "ck_task_criteria_lists_state."
            ),
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment=(
                "When the current claim started; renewed when an abandoned "
                "claim is taken over. The takeover and the final write compare it, so "
                "a claimer that lost its claim cannot write over the new claimer's "
                "list."
            ),
        ),
        sa.Column(
            "form_version",
            sa.SmallInteger(),
            nullable=False,
            comment=(
                "Version of the criterion form the list is composed in — the "
                "response contract of the decomposition prompt (2: task 08)."
            ),
        ),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "The criteria of this task version as one document: each "
                "criterion with its id, text, evidence, weight category, check method, "
                "soft-descent mark, main concepts and mandatory items. NULL until "
                "READY."
            ),
        ),
        sa.Column(
            "contradictions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Contradictions between the task and its node description found "
                "by the same call — for the author only, never in criteria or score. "
                "[] — none found; NULL until READY."
            ),
        ),
        sa.Column(
            "concepts_in_input",
            sa.Boolean(),
            nullable=True,
            comment=(
                "Whether a node or root summary — the source of concepts — was "
                "available to the composition. A list composed without one is "
                "recomposed exactly once when one appears; after that the flag stays "
                "true even if the concepts later disappear. NULL until READY."
            ),
        ),
        sa.Column(
            "dropped_concept_count",
            sa.Integer(),
            nullable=True,
            comment=(
                "Concepts the model named that were not in its input — dropped "
                "by code and counted. NULL until READY."
            ),
        ),
        sa.Column(
            "input_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment=(
                "SHA-256 of the input context (task content hash, node "
                "description, concept lists, prompt hash) — diagnostics only, never a "
                "version key. NULL until READY."
            ),
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment="Why the composition gave up (state='failed'). NULL otherwise.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_SOFT_DELETE_COMMENT,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'failed')",
            name="ck_task_criteria_lists_state",
        ),
        sa.CheckConstraint(
            "state <> 'ready' OR (criteria IS NOT NULL "
            "AND contradictions IS NOT NULL AND concepts_in_input IS NOT NULL "
            "AND dropped_concept_count IS NOT NULL "
            "AND input_fingerprint IS NOT NULL)",
            name="ck_task_criteria_lists_ready_complete",
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR deleted_at IS NOT NULL",
            name="ck_task_criteria_lists_failed_is_history",
        ),
        sa.ForeignKeyConstraint(
            ["authored_document_id"], ["authored_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "The criteria list of a task version (mentor-rebuild task 08) — "
            "the machine layer, with a lifecycle (pending → ready | failed) "
            "so that several workers share one composition. One live row — "
            "a claim or a ready list — per task; earlier versions are "
            "soft-deleted history."
        ),
    )
    op.create_index(
        "ix_task_criteria_lists_authored_document_id",
        _LISTS,
        ["authored_document_id"],
    )
    op.create_index(
        "ix_task_criteria_lists_active",
        _LISTS,
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # A partial index does not round-trip through autogenerate: this migration,
    # not a generated one, is the authority for its predicate.
    op.create_index(
        "uq_task_criteria_lists_authored_document_id_active",
        _LISTS,
        ["authored_document_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        _OVERRIDES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the task). Hard-delete cascades with the "
                "document; soft-delete cascades via "
                "AuthoredDocument.__cascades_soft_delete_to__. One live edit per task "
                "(uq_task_criteria_overrides_authored_document_id_active)."
            ),
        ),
        sa.Column(
            "source_content_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "AuthoredDocument.content_hash of the task version this edit "
                "was written for. The edit is in force only while it equals the live "
                "one; it is never carried to a new version."
            ),
        ),
        sa.Column(
            "source_task_type",
            sa.String(length=32),
            nullable=False,
            comment=(
                "AuthoredDocument.task_type of the task version this edit was "
                "written for; re-typing the task leaves the edit out of force."
            ),
        ),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment=(
                "The author's criteria, replaced whole — the criterion form of "
                "task_criteria_lists.criteria; ids of edited criteria are kept, new "
                "criteria get new ones."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_SOFT_DELETE_COMMENT,
        ),
        sa.CheckConstraint(
            "criteria <> '[]'::jsonb",
            name="ck_task_criteria_overrides_criteria_present",
        ),
        sa.ForeignKeyConstraint(
            ["authored_document_id"], ["authored_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "The author's edit of a task's criteria list (mentor-rebuild "
            "task 08) — replaced whole, bound to one task version, "
            "soft-deleted on replacement or reset so every earlier state "
            "stays as a snapshot. One live edit per task."
        ),
    )
    op.create_index(
        "ix_task_criteria_overrides_authored_document_id",
        _OVERRIDES,
        ["authored_document_id"],
    )
    op.create_index(
        "ix_task_criteria_overrides_active",
        _OVERRIDES,
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_task_criteria_overrides_authored_document_id_active",
        _OVERRIDES,
        ["authored_document_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.alter_column(
        _REFERENCES,
        "kind",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        comment=_KIND_AFTER,
        existing_comment=_KIND_BEFORE,
    )


def downgrade() -> None:
    """Put the comment back, then drop the author's edit and the list."""
    op.alter_column(
        _REFERENCES,
        "kind",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        comment=_KIND_BEFORE,
        existing_comment=_KIND_AFTER,
    )
    op.drop_index(
        "uq_task_criteria_overrides_authored_document_id_active",
        table_name=_OVERRIDES,
    )
    op.drop_index("ix_task_criteria_overrides_active", table_name=_OVERRIDES)
    op.drop_index(
        "ix_task_criteria_overrides_authored_document_id", table_name=_OVERRIDES
    )
    op.drop_table(_OVERRIDES)
    op.drop_index(
        "uq_task_criteria_lists_authored_document_id_active", table_name=_LISTS
    )
    op.drop_index("ix_task_criteria_lists_active", table_name=_LISTS)
    op.drop_index("ix_task_criteria_lists_authored_document_id", table_name=_LISTS)
    op.drop_table(_LISTS)
