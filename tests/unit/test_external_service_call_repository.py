"""Unit tests for ExternalServiceCallRepository (KD5 cost-report queries).

Contract-level coverage with AsyncMock — SQL semantics (real GROUP BY,
JOIN, NULL exclusion) live in the integration suite. Here we verify
each method calls ``session.execute`` once with the correct shape,
projects rows into the right NamedTuple, and short-circuits on empty
subtree without emitting SQL.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.storage.repositories import (
    ByActionRow,
    ByCourseRow,
    ByNodeRow,
    ByProviderRow,
    ExternalServiceCallRepository,
    _to_exclusive,
)


def _scalar_session(value: float | int) -> AsyncMock:
    """Build an AsyncMock session whose execute().scalar_one() returns value."""
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _rows_session(rows: list[MagicMock]) -> AsyncMock:
    """Build an AsyncMock session whose execute().all() returns rows."""
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


FROM = date(2026, 1, 1)
TO = date(2026, 4, 28)


class TestToExclusive:
    """Boundary helper used by every breakdown method."""

    def test_adds_one_day(self) -> None:
        assert _to_exclusive(date(2026, 4, 28)) == date(2026, 4, 29)

    def test_month_rollover(self) -> None:
        assert _to_exclusive(date(2026, 1, 31)) == date(2026, 2, 1)


class TestGetTotalForPeriod:
    async def test_returns_float_from_scalar_one(self) -> None:
        session = _scalar_session(150.5)
        repo = ExternalServiceCallRepository(session)
        total = await repo.get_total_for_period(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert total == pytest.approx(150.5)
        session.execute.assert_awaited_once()

    async def test_zero_when_scalar_zero(self) -> None:
        session = _scalar_session(0.0)
        repo = ExternalServiceCallRepository(session)
        total = await repo.get_total_for_period(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert total == 0.0


class TestGetTotalForSubtree:
    async def test_empty_subtree_short_circuits_without_sql(self) -> None:
        session = AsyncMock()
        repo = ExternalServiceCallRepository(session)
        total = await repo.get_total_for_subtree(
            tenant_id=uuid.uuid4(),
            course_node_ids=[],
            from_date=FROM,
            to_date=TO,
        )
        assert total == 0.0
        session.execute.assert_not_called()

    async def test_non_empty_subtree_executes_query(self) -> None:
        session = _scalar_session(42.0)
        repo = ExternalServiceCallRepository(session)
        total = await repo.get_total_for_subtree(
            tenant_id=uuid.uuid4(),
            course_node_ids=[uuid.uuid4()],
            from_date=FROM,
            to_date=TO,
        )
        assert total == pytest.approx(42.0)
        session.execute.assert_awaited_once()


class TestGetUnattributedForPeriod:
    async def test_returns_float(self) -> None:
        session = _scalar_session(5.0)
        repo = ExternalServiceCallRepository(session)
        result = await repo.get_unattributed_for_period(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert result == pytest.approx(5.0)
        session.execute.assert_awaited_once()

    async def test_zero_when_no_rows(self) -> None:
        session = _scalar_session(0.0)
        repo = ExternalServiceCallRepository(session)
        result = await repo.get_unattributed_for_period(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert result == 0.0


class TestGetByCourse:
    async def test_projects_rows_into_namedtuples(self) -> None:
        course_id = uuid.uuid4()
        row = MagicMock()
        row.course_node_id = course_id
        row.course_title = "Python Basics"
        row.cost_usd = 100.0
        session = _rows_session([row])
        repo = ExternalServiceCallRepository(session)

        results = await repo.get_by_course(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )

        assert results == [
            ByCourseRow(
                course_node_id=course_id, course_title="Python Basics", cost_usd=100.0
            )
        ]

    async def test_empty_result_returns_empty_list(self) -> None:
        session = _rows_session([])
        repo = ExternalServiceCallRepository(session)
        results = await repo.get_by_course(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert results == []


class TestGetByProvider:
    async def test_projects_provider_model_pairs(self) -> None:
        row = MagicMock()
        row.provider = "anthropic"
        row.model_id = "claude-sonnet-4"
        row.cost_usd = 75.0
        session = _rows_session([row])
        repo = ExternalServiceCallRepository(session)

        results = await repo.get_by_provider(
            tenant_id=uuid.uuid4(), from_date=FROM, to_date=TO
        )
        assert results == [
            ByProviderRow(
                provider="anthropic", model_id="claude-sonnet-4", cost_usd=75.0
            )
        ]


class TestGetByNode:
    async def test_empty_subtree_short_circuits(self) -> None:
        session = AsyncMock()
        repo = ExternalServiceCallRepository(session)
        results = await repo.get_by_node(
            tenant_id=uuid.uuid4(),
            course_node_ids=[],
            from_date=FROM,
            to_date=TO,
        )
        assert results == []
        session.execute.assert_not_called()

    async def test_projects_node_rows(self) -> None:
        node_id = uuid.uuid4()
        row = MagicMock()
        row.course_node_id = node_id
        row.title = "Lesson 1"
        row.cost_usd = 30.0
        session = _rows_session([row])
        repo = ExternalServiceCallRepository(session)

        results = await repo.get_by_node(
            tenant_id=uuid.uuid4(),
            course_node_ids=[uuid.uuid4()],
            from_date=FROM,
            to_date=TO,
        )
        assert results == [
            ByNodeRow(course_node_id=node_id, title="Lesson 1", cost_usd=30.0)
        ]


class TestGetByAction:
    async def test_empty_subtree_short_circuits(self) -> None:
        session = AsyncMock()
        repo = ExternalServiceCallRepository(session)
        results = await repo.get_by_action(
            tenant_id=uuid.uuid4(),
            course_node_ids=[],
            from_date=FROM,
            to_date=TO,
        )
        assert results == []
        session.execute.assert_not_called()

    async def test_projects_action_rows(self) -> None:
        row = MagicMock()
        row.action = "document_processing"
        row.cost_usd = 60.0
        session = _rows_session([row])
        repo = ExternalServiceCallRepository(session)

        results = await repo.get_by_action(
            tenant_id=uuid.uuid4(),
            course_node_ids=[uuid.uuid4()],
            from_date=FROM,
            to_date=TO,
        )
        assert results == [ByActionRow(action="document_processing", cost_usd=60.0)]


class TestPaginationParametersThreaded:
    """Limit/offset reach the compiled statement (don't get silently dropped).

    Inspect the SQL string of the captured statement — the breakdown
    methods must end with ``LIMIT/OFFSET`` clauses.
    """

    @pytest.mark.parametrize(
        ("method", "kwargs"),
        [
            (
                "get_by_course",
                {"tenant_id": uuid.uuid4(), "from_date": FROM, "to_date": TO},
            ),
            (
                "get_by_provider",
                {"tenant_id": uuid.uuid4(), "from_date": FROM, "to_date": TO},
            ),
            (
                "get_by_node",
                {
                    "tenant_id": uuid.uuid4(),
                    "course_node_ids": [uuid.uuid4()],
                    "from_date": FROM,
                    "to_date": TO,
                },
            ),
            (
                "get_by_action",
                {
                    "tenant_id": uuid.uuid4(),
                    "course_node_ids": [uuid.uuid4()],
                    "from_date": FROM,
                    "to_date": TO,
                },
            ),
        ],
    )
    async def test_limit_offset_appear_in_compiled_sql(
        self, method: str, kwargs: dict[str, object]
    ) -> None:
        session = _rows_session([])
        repo = ExternalServiceCallRepository(session)
        await getattr(repo, method)(**kwargs, limit=33, offset=7)

        stmt = session.execute.call_args[0][0]
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "LIMIT 33" in sql
        assert "OFFSET 7" in sql
