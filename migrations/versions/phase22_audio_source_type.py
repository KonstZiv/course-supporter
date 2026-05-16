"""Phase 2.2 sub-area #6: extend source_type_enum with 'audio' value.

Revision ID: phase22_audio_source_type
Revises: phase21_drop_structure_family
Create Date: 2026-05-16 00:00:00.000000

Adds the 'audio' value to the existing PostgreSQL ENUM ``source_type_enum``
that backs ``authored_documents.source_type``. The ORM-side literal list in
``src/course_supporter/storage/orm.py`` was extended in the same sub-area
commit; this migration is the authoritative PG-side counterpart.

``ALTER TYPE ... ADD VALUE`` is wrapped in ``IF NOT EXISTS`` so re-running
the migration against a database that already carries the value is a
no-op, matching the operator-friendly idempotency convention used elsewhere
in the migration chain.

Downgrade is intentionally NOT implemented. PostgreSQL does not support
removing a value from an existing ENUM type without recreating the type
in place (drop column references → drop type → recreate without the value
→ restore references), which is a destructive cross-table operation that
does not belong in a sub-area-scoped reverse migration. If audio support
must be retired, that is a phase-scoped redesign, not a column-level
rollback of this migration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "phase22_audio_source_type"
down_revision: str | Sequence[str] | None = "phase21_drop_structure_family"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend source_type_enum with 'audio' value (idempotent)."""
    op.execute("ALTER TYPE source_type_enum ADD VALUE IF NOT EXISTS 'audio'")


def downgrade() -> None:
    """Reverse migration is intentionally unsupported.

    See module docstring for rationale.
    """
    raise NotImplementedError(
        "PostgreSQL does not support dropping enum values without recreating the type."
    )
