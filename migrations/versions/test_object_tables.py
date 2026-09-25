"""mentor-rebuild task 07b, commit A2: the test's draft, its published versions, a title.

Revision ID: test_object_tables
Revises: test_object_source_type
Create Date: 2026-09-25

A test becomes an object the author writes in the system (task 07b). One
revision, because what it adds arrives together and is read by the same new
code:

1. ``test_drafts`` — the draft, one row per test (``uq_test_draft_document``),
   replaced whole. Students never read it.
2. ``test_versions`` — the published versions, frozen and append-only, each with
   three digests (``storage.orm.TestVersion``). A publication is compared with
   the latest version only, so the whole-publication digest is NOT unique:
   returning to an earlier state is a new version (task 07b, decision 6,
   clarified 2026-09-25). ``uq_test_version_document_version`` numbers the
   versions per test and settles two publications racing for one number.
3. ``authored_documents.title`` — ``VARCHAR(200) NULL``, the name both trees show.
   A test written in the system has no file name to show instead.
4. The comment of ``task_references.source_content_hash`` gains its second
   meaning: for a test object, the content axis is the visible digest of the
   published version, not ``AuthoredDocument.content_hash``. Without this the
   comment would lie.

Both new tables are plain: no soft delete of their own — a draft and its
versions go only with their test (FK ON DELETE CASCADE), and "hiding" a test is
the soft delete of its document.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``). The ORM mirror is ``storage.orm.TestDraft``,
``storage.orm.TestVersion`` and ``AuthoredDocument.title``. Adding tables and a
nullable column rewrites no row and fails none. Downgrade drops them and puts the
old comment back; lossless ONLY on a database where nothing has written them yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "test_object_tables"
down_revision: str | Sequence[str] | None = "test_object_source_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOCUMENTS = "authored_documents"
_DRAFTS = "test_drafts"
_VERSIONS = "test_versions"
_REFERENCES = "task_references"

_SOURCE_HASH_BEFORE = (
    "Content-axis version key = AuthoredDocument.content_hash at generation time "
    "(mirrors TaskCriteria.source_content_hash)."
)
_SOURCE_HASH_AFTER = (
    "Content-axis version key = AuthoredDocument.content_hash at generation time "
    "(mirrors TaskCriteria.source_content_hash); for a test object (task 07b), "
    "the visible digest of its published version."
)


def upgrade() -> None:
    """Add the title, the draft and the published versions; widen one comment."""
    op.add_column(
        _DOCUMENTS,
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=True,
            comment=(
                "The name both trees show (task 07b). NULL — a material with no "
                "name of its own: its label is the file name."
            ),
        ),
    )

    op.create_table(
        _DRAFTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the test). CASCADE. One draft per test "
                "(uq_test_draft_document, which also covers this FK)."
            ),
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment=(
                "The checked draft: pass_threshold, and questions — each with its "
                "text, its options (text and correct) and an optional explanation. "
                "No option letters: those are set at publication."
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
        sa.ForeignKeyConstraint(
            ["authored_document_id"], ["authored_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "The draft of a test written in the system (mentor-rebuild task 07b): "
            "one row per test, replaced whole, never read by students."
        ),
    )
    op.create_index(
        "uq_test_draft_document", _DRAFTS, ["authored_document_id"], unique=True
    )

    op.create_table(
        _VERSIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "authored_document_id",
            sa.Uuid(),
            nullable=False,
            comment=(
                "FK → AuthoredDocument (the test). CASCADE. Covered by the "
                "composite indexes (leftmost prefix), so no standalone index."
            ),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment=(
                "Monotonic 1-based version per test. A publication that differs "
                "is a new version; an existing one is never rewritten."
            ),
        ),
        sa.Column(
            "language",
            sa.String(length=10),
            nullable=False,
            comment=(
                "The language the option letters were set by, ISO 639-3 (the "
                "course language at publication)."
            ),
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment=(
                "The published test, frozen: pass_threshold, and questions — each "
                "with its number, text, options (letter, text, correct) and "
                "optional explanation."
            ),
        ),
        sa.Column(
            "content_digest",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of what the student sees: numbers, texts, letters. The "
                "structure route's version; the content axis of the explanations."
            ),
        ),
        sa.Column(
            "answers_digest",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of the key in canonical form — the answers axis of the "
                "explanations (reference_key.answers_digest)."
            ),
        ),
        sa.Column(
            "publication_digest",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of everything published — the pass mark and the author's "
                "own explanations included. Compared with the latest version's at "
                "publication; equal — no new version."
            ),
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["authored_document_id"], ["authored_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "Published versions of a test written in the system (mentor-rebuild "
            "task 07b): frozen, append-only; a new one whenever a publication "
            "differs from the latest."
        ),
    )
    op.create_index(
        "uq_test_version_document_version",
        _VERSIONS,
        ["authored_document_id", "version"],
        unique=True,
    )
    op.create_index(
        "ix_test_versions_document_content",
        _VERSIONS,
        ["authored_document_id", "content_digest"],
    )

    op.alter_column(
        _REFERENCES,
        "source_content_hash",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        comment=_SOURCE_HASH_AFTER,
        existing_comment=_SOURCE_HASH_BEFORE,
    )


def downgrade() -> None:
    """Put the comment back, then drop the versions, the draft and the title."""
    op.alter_column(
        _REFERENCES,
        "source_content_hash",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        comment=_SOURCE_HASH_BEFORE,
        existing_comment=_SOURCE_HASH_AFTER,
    )
    op.drop_index("ix_test_versions_document_content", table_name=_VERSIONS)
    op.drop_index("uq_test_version_document_version", table_name=_VERSIONS)
    op.drop_table(_VERSIONS)
    op.drop_index("uq_test_draft_document", table_name=_DRAFTS)
    op.drop_table(_DRAFTS)
    op.drop_column(_DOCUMENTS, "title")
