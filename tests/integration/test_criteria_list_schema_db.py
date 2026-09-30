"""The comments task 08 leaves in the database are the ORM's (task 08, K1).

Requires ``docker compose up -d`` (PostgreSQL), migrated to head.

``test_schema_sync`` checks tables and columns but not comments (``DD-L4-A``),
and a migration copies each comment from the ORM by hand — a drift between the
two would pass every other test and leave the database saying one thing and the
code another. For the two tables of task 08, and for the one comment it narrows
(``task_references.kind``), this reads the live comments and compares them with
the ORM's, table and column by column.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import (
    TaskCriteriaList,
    TaskCriteriaOverride,
    TaskReference,
)

pytestmark = pytest.mark.requires_db

_TASK_08_TABLES = [
    cast(Table, TaskCriteriaList.__table__),
    cast(Table, TaskCriteriaOverride.__table__),
]


async def _column_comments(
    session: AsyncSession, table_name: str
) -> dict[str, str | None]:
    result = await session.execute(
        text(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": table_name},
    )
    return {str(name): comment for name, comment in result.all()}


class TestCommentsAreTheOrms:
    @pytest.mark.parametrize("table", _TASK_08_TABLES, ids=lambda t: t.name)
    async def test_every_column_comment_is_the_orms(
        self, db_session: AsyncSession, table: Table
    ) -> None:
        live = await _column_comments(db_session, table.name)

        # The premise: the live table has exactly the ORM's columns, so the
        # comparison below walks every one of them.
        assert set(live) == {column.name for column in table.columns}
        for column in table.columns:
            assert live[column.name] == column.comment, column.name

    @pytest.mark.parametrize("table", _TASK_08_TABLES, ids=lambda t: t.name)
    async def test_the_table_comment_is_the_orms(
        self, db_session: AsyncSession, table: Table
    ) -> None:
        live = await db_session.execute(
            text("SELECT obj_description(CAST(:table AS regclass), 'pg_class')"),
            {"table": table.name},
        )

        assert live.scalar_one() == table.comment

    async def test_the_reference_kind_comment_no_longer_plans_mandatory_points(
        self, db_session: AsyncSession
    ) -> None:
        """Task 08 keeps mandatory items inside the criteria list, not as a kind
        of reference (section 3.3), so the column comment says no such plan."""
        live = await _column_comments(db_session, "task_references")
        orm_comment = cast(Table, TaskReference.__table__).c.kind.comment

        assert live["kind"] == orm_comment
        assert "mandatory_points" not in (live["kind"] or "")
