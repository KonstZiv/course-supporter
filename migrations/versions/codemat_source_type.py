"""task-code-materials commit 1: extend source_type_enum with 'code' value.

Revision ID: codemat_source_type
Revises: project_bases_p2
Create Date: 2026-07-12 00:00:00.000000

Adds the 'code' value to the existing PostgreSQL ENUM ``source_type_enum``
that backs ``authored_documents.source_type``. The ORM-side literal list in
``src/course_supporter/storage/orm.py`` was extended in the same commit;
this migration is the authoritative PG-side counterpart (same split as the
audio precedent, ``phase22_audio_source_type``).

``ALTER TYPE ... ADD VALUE`` is wrapped in ``IF NOT EXISTS`` so re-running
the migration against a database that already carries the value is a
no-op, matching the operator-friendly idempotency convention used elsewhere
in the migration chain.

Downgrade is intentionally NOT implemented. PostgreSQL does not support
removing a value from an existing ENUM type without recreating the type
in place (drop column references → drop type → recreate without the value
→ restore references), which is a destructive cross-table operation that
does not belong in a task-scoped reverse migration. Accepted with an
explicit flag in the ratified task contract (F-record "Дрібниці",
2026-07-11), per the phase22 precedent.

The task's new nullable columns (DocumentSegment.file_path,
DocumentSummary.structure, Job.error_category,
AuthoredDocument.error_category) ship in a SEPARATE later revision per the
ratified contract — this revision is enum-only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "codemat_source_type"
down_revision: str | Sequence[str] | None = "project_bases_p2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend source_type_enum with 'code' value (idempotent)."""
    op.execute("ALTER TYPE source_type_enum ADD VALUE IF NOT EXISTS 'code'")


def downgrade() -> None:
    """Reverse migration is intentionally unsupported.

    See module docstring for rationale.
    """
    raise NotImplementedError(
        "PostgreSQL does not support dropping enum values without recreating the type."
    )
