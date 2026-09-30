"""mentor-rebuild task 08, commit K6: ``task_criteria`` is an archive.

Revision ID: task_criteria_archive_comment
Revises: criteria_claim_heartbeat
Create Date: 2026-09-30

Since commit K4 no review reads ``task_criteria``: the criteria of a task version
live in ``task_criteria_lists`` and ``task_criteria_overrides``, and the old
table stays only as an archive the soft-delete cascade still walks (task 08,
section 9, decisions 9 and 24). Its table comment still said the review graph
reuses it read-through; this puts the ORM's comment in its place.

A comment only: no column, constraint or row changes, so both directions are
lossless. Hand-written (SPRINT rule 6 of §2); ``--autogenerate`` is not used on
this database (``DD-L4-A``). The ORM mirror is the table comment of
``storage.orm.TaskCriteria``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "task_criteria_archive_comment"
down_revision: str | Sequence[str] | None = "criteria_claim_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE = (
    "Cached task→criteria decomposition (vision §1386, D11) — one ACTIVE row "
    "per task version; reused read-through by the Mentor review graph (T6)"
)
_AFTER = (
    "Archive since mentor-rebuild task 08: the cached task→criteria "
    "decomposition (vision §1386, D11) reviews read until then — one ACTIVE row "
    "per task version; the column comments describe the table as it worked "
    "then. No code reads or writes it but the soft-delete cascade, which marks "
    "its rows deleted with their task; criteria lists live in "
    "task_criteria_lists."
)


def upgrade() -> None:
    """Say that the table is an archive."""
    op.create_table_comment("task_criteria", _AFTER, existing_comment=_BEFORE)


def downgrade() -> None:
    """Put back the comment the table was created with (sprint-mentor T4)."""
    op.create_table_comment("task_criteria", _BEFORE, existing_comment=_AFTER)
