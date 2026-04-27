"""0.1 soft-delete infrastructure: deleted_at column + protection trigger

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-04-27 12:24:42.000000

Task 0.1 of the Phase 0 sprint (vision §3 KD3, KD12). Introduces the
universal soft-delete primitive across every soft-deletable table:

- ``deleted_at TIMESTAMPTZ NULL`` column on each listed table
  (NULL = active, NOT NULL = soft-deleted).
- Partial index ``ix_<table>_active`` on ``deleted_at IS NULL`` —
  cheap "active-only" filtering without bloating the index with
  deleted rows.
- A single ``block_update_on_soft_deleted()`` plpgsql function and a
  ``BEFORE UPDATE`` trigger applied to every soft-deletable table.
  The trigger blocks any UPDATE on a row whose ``deleted_at IS NOT
  NULL`` unless the only column actually changing is ``deleted_at``
  itself (this preserves un-delete and idempotent re-delete via raw
  SQL — see the soft-delete repository for the application-level
  early-return path).

Concrete cascade maps and per-entity content scrubbing are NOT
configured here; they are wired with each entity's own phase task
(1.x, 3.x, 4.x).
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SOFT_DELETABLE_TABLES: tuple[str, ...] = (
    "tenants",
    "api_keys",
    "material_nodes",
    "material_entries",
    "material_macro_sections",
    "material_segments",
    "jobs",
    "students",
    "homework_submissions",
)

TRIGGER_FUNCTION_NAME = "block_update_on_soft_deleted"

# PostgreSQL DDL cannot use parameter binding for identifiers
# (``CREATE TRIGGER $1 ON $2`` is not valid SQL — bind params attach
# values, not object names), so trigger and table names below are
# interpolated into raw SQL via f-strings. All names come from the
# hardcoded ``SOFT_DELETABLE_TABLES`` whitelist or ``TRIGGER_FUNCTION_NAME``;
# user input never reaches these strings. The assertions below make
# that property executable: if a future contributor adds a non-plain
# identifier to the whitelist, the migration fails on import rather
# than emitting unsafe DDL.
_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

assert all(_SAFE_IDENT.match(t) for t in SOFT_DELETABLE_TABLES), (
    "SOFT_DELETABLE_TABLES must contain plain SQL identifiers; got: "
    f"{[t for t in SOFT_DELETABLE_TABLES if not _SAFE_IDENT.match(t)]}"
)
assert _SAFE_IDENT.match(TRIGGER_FUNCTION_NAME), (
    f"TRIGGER_FUNCTION_NAME must be a plain SQL identifier; got: "
    f"{TRIGGER_FUNCTION_NAME!r}"
)

TRIGGER_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {TRIGGER_FUNCTION_NAME}()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.deleted_at IS NOT NULL THEN
        IF (to_jsonb(NEW) - 'deleted_at')
            IS DISTINCT FROM (to_jsonb(OLD) - 'deleted_at') THEN
            RAISE EXCEPTION
                'Cannot UPDATE soft-deleted row in %.%: '
                'only deleted_at may change while deleted_at IS NOT NULL',
                TG_TABLE_SCHEMA, TG_TABLE_NAME;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _trigger_name(table: str) -> str:
    return f"trg_{table}_block_update_when_deleted"


def upgrade() -> None:
    """Add deleted_at + partial index + protection trigger on each table."""
    for table in SOFT_DELETABLE_TABLES:
        op.add_column(
            table,
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment=(
                    "Soft-delete timestamp (vision KD3). "
                    "NULL = active; NOT NULL = soft-deleted."
                ),
            ),
        )
        op.create_index(
            f"ix_{table}_active",
            table,
            ["deleted_at"],
            unique=False,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )

    op.execute(TRIGGER_FUNCTION_SQL)

    for table in SOFT_DELETABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {_trigger_name(table)} "
            f"BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FUNCTION_NAME}();"
        )


def downgrade() -> None:
    """Drop triggers, function, partial indexes, and the deleted_at column."""
    for table in SOFT_DELETABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {_trigger_name(table)} ON {table};")

    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION_NAME}();")

    for table in SOFT_DELETABLE_TABLES:
        op.drop_index(f"ix_{table}_active", table_name=table)
        op.drop_column(table, "deleted_at")
