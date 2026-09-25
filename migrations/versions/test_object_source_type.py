"""mentor-rebuild task 07b, commit A1: extend source_type_enum with 'test_object'.

Revision ID: test_object_source_type
Revises: test_reference_threshold_doubts
Create Date: 2026-09-25

A test becomes an object the author writes in the system (task 07b): its
questions, options, pass mark and explanations live in the database, not in a
file that is processed. Its ``authored_documents`` row still needs a source
type, and none of the six says "nothing to process": ``test_object`` is that
value. The ingestion factory has no processor for it, so a row of this type
that reaches the processing worker by accident is refused as an unsupported
source type before the safety stage — before anything is paid for.

Enum-only, like ``codemat_source_type``: PostgreSQL cannot use an enum value in
the transaction that adds it, so the tables that follow (the test's draft and
its published versions) come in the next revision. The ORM-side literal list in
``storage/orm.py`` and ``models.source.SourceType`` are extended in the same
commit; this revision is the database side.

``ADD VALUE IF NOT EXISTS`` makes a re-run a no-op. Downgrade is not
implemented, as in ``codemat_source_type`` and ``phase22_audio_source_type``:
PostgreSQL cannot drop a value from an enum without recreating the type across
every column that uses it.

Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on this
database (``DD-L4-A``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "test_object_source_type"
down_revision: str | Sequence[str] | None = "test_reference_threshold_doubts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add 'test_object' to source_type_enum (idempotent)."""
    op.execute("ALTER TYPE source_type_enum ADD VALUE IF NOT EXISTS 'test_object'")


def downgrade() -> None:
    """Reverse migration is intentionally unsupported.

    See the module docstring for why.
    """
    raise NotImplementedError(
        "PostgreSQL does not support dropping enum values without recreating the type."
    )
