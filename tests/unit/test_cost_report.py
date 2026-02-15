"""Tests for cost report: repository, API endpoint, and CLI output."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.models.reports import CostReport, CostSummary, GroupedCost
from course_supporter.storage.repositories import LLMCallRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=[],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _mock_row(**kwargs: Any) -> MagicMock:
    """Create a mock row with named attributes."""
    row = MagicMock()
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def _empty_summary_row() -> MagicMock:
    return _mock_row(
        total_calls=0,
        successful_calls=0,
        failed_calls=0,
        total_cost_usd=0.0,
        total_tokens_in=0,
        total_tokens_out=0,
        avg_latency_ms=0.0,
    )


def _filled_summary_row() -> MagicMock:
    return _mock_row(
        total_calls=5,
        successful_calls=4,
        failed_calls=1,
        total_cost_usd=0.0123,
        total_tokens_in=5000,
        total_tokens_out=2000,
        avg_latency_ms=450.5,
    )


def _grouped_rows(groups: list[dict[str, Any]]) -> list[MagicMock]:
    return [_mock_row(**g) for g in groups]


class TestLLMCallRepositorySummary:
    """Tests for LLMCallRepository.get_summary."""

    async def test_get_summary_empty_table(self) -> None:
        """Empty table returns zeroes."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _empty_summary_row()
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        summary = await repo.get_summary()

        assert summary.total_calls == 0
        assert summary.total_cost_usd == 0.0
        assert summary.total_tokens_in == 0
        assert summary.avg_latency_ms == 0.0

    async def test_get_summary_with_data(self) -> None:
        """Filled table returns correct aggregation."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _filled_summary_row()
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        summary = await repo.get_summary()

        assert summary.total_calls == 5
        assert summary.successful_calls == 4
        assert summary.failed_calls == 1
        assert summary.total_cost_usd == pytest.approx(0.0123)
        assert summary.total_tokens_in == 5000
        assert summary.total_tokens_out == 2000
        assert summary.avg_latency_ms == pytest.approx(450.5)


class TestLLMCallRepositoryGrouped:
    """Tests for grouped cost queries."""

    async def test_get_by_action_groups(self) -> None:
        """GROUP BY action returns correct groups."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = _grouped_rows(
            [
                {
                    "group": "architect",
                    "calls": 3,
                    "cost_usd": 0.01,
                    "tokens_in": 3000,
                    "tokens_out": 1500,
                    "avg_latency_ms": 400.0,
                },
                {
                    "group": "summarize",
                    "calls": 2,
                    "cost_usd": 0.005,
                    "tokens_in": 2000,
                    "tokens_out": 500,
                    "avg_latency_ms": 300.0,
                },
            ]
        )
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        groups = await repo.get_by_action()

        assert len(groups) == 2
        assert groups[0].group == "architect"
        assert groups[0].calls == 3
        assert groups[1].group == "summarize"

    async def test_get_by_provider_groups(self) -> None:
        """GROUP BY provider returns correct groups."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = _grouped_rows(
            [
                {
                    "group": "gemini",
                    "calls": 4,
                    "cost_usd": 0.008,
                    "tokens_in": 4000,
                    "tokens_out": 1800,
                    "avg_latency_ms": 350.0,
                },
            ]
        )
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        groups = await repo.get_by_provider()

        assert len(groups) == 1
        assert groups[0].group == "gemini"
        assert groups[0].calls == 4

    async def test_get_by_model_groups(self) -> None:
        """GROUP BY model_id returns correct groups."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = _grouped_rows(
            [
                {
                    "group": "gemini-2.0-flash",
                    "calls": 3,
                    "cost_usd": 0.006,
                    "tokens_in": 3000,
                    "tokens_out": 1500,
                    "avg_latency_ms": 400.0,
                },
            ]
        )
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        groups = await repo.get_by_model()

        assert len(groups) == 1
        assert groups[0].group == "gemini-2.0-flash"


class TestCostReportAPI:
    """Tests for GET /api/v1/reports/cost."""

    @pytest.fixture()
    def _mock_repo(self) -> CostReport:
        """Return a fixture CostReport."""
        return CostReport(
            summary=CostSummary(
                total_calls=2,
                successful_calls=2,
                failed_calls=0,
                total_cost_usd=0.005,
                total_tokens_in=1000,
                total_tokens_out=500,
                avg_latency_ms=200.0,
            ),
            by_action=[
                GroupedCost(
                    group="architect",
                    calls=2,
                    cost_usd=0.005,
                    tokens_in=1000,
                    tokens_out=500,
                    avg_latency_ms=200.0,
                ),
            ],
            by_provider=[],
            by_model=[],
        )

    async def test_api_cost_report_200(self, _mock_repo: CostReport) -> None:
        """GET /api/v1/reports/cost returns 200."""
        from course_supporter.api.app import app

        @asynccontextmanager
        async def mock_session_ctx() -> AsyncIterator[AsyncMock]:
            session = AsyncMock()
            yield session

        app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
        try:
            with (
                patch(
                    "course_supporter.api.routes.reports.async_session",
                    mock_session_ctx,
                ),
                patch(
                    "course_supporter.api.routes.reports.LLMCallRepository",
                ) as mock_repo_cls,
            ):
                repo_instance = AsyncMock()
                repo_instance.get_full_report.return_value = _mock_repo
                mock_repo_cls.return_value = repo_instance

                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    response = await client.get("/api/v1/reports/cost")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200

    async def test_api_cost_report_response_schema(
        self, _mock_repo: CostReport
    ) -> None:
        """Response matches CostReport schema."""
        from course_supporter.api.app import app

        @asynccontextmanager
        async def mock_session_ctx() -> AsyncIterator[AsyncMock]:
            session = AsyncMock()
            yield session

        app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
        try:
            with (
                patch(
                    "course_supporter.api.routes.reports.async_session",
                    mock_session_ctx,
                ),
                patch(
                    "course_supporter.api.routes.reports.LLMCallRepository",
                ) as mock_repo_cls,
            ):
                repo_instance = AsyncMock()
                repo_instance.get_full_report.return_value = _mock_repo
                mock_repo_cls.return_value = repo_instance

                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    response = await client.get("/api/v1/reports/cost")
        finally:
            app.dependency_overrides.clear()

        data = response.json()
        report = CostReport.model_validate(data)
        assert report.summary.total_calls == 2
        assert len(report.by_action) == 1
        assert report.by_action[0].group == "architect"


class TestCostReportCLI:
    """Tests for CLI output formatting."""

    def test_cli_table_output(self) -> None:
        """print_table returns formatted ASCII table."""
        from scripts.cost_report import print_table

        report = CostReport(
            summary=CostSummary(
                total_calls=3,
                successful_calls=3,
                failed_calls=0,
                total_cost_usd=0.0075,
                total_tokens_in=3000,
                total_tokens_out=1500,
                avg_latency_ms=300.0,
            ),
            by_action=[
                GroupedCost(
                    group="architect",
                    calls=3,
                    cost_usd=0.0075,
                    tokens_in=3000,
                    tokens_out=1500,
                    avg_latency_ms=300.0,
                ),
            ],
            by_provider=[],
            by_model=[],
        )
        table = print_table(report)
        assert "Cost Summary" in table
        assert "3000" in table
        assert "architect" in table
        assert "By Action" in table

    def test_cli_json_output(self) -> None:
        """CostReport serializes to valid JSON."""
        import json

        report = CostReport(
            summary=CostSummary(
                total_calls=1,
                successful_calls=1,
                failed_calls=0,
                total_cost_usd=0.001,
                total_tokens_in=100,
                total_tokens_out=50,
                avg_latency_ms=100.0,
            ),
            by_action=[],
            by_provider=[],
            by_model=[],
        )
        output = json.dumps(report.model_dump(), indent=2)
        parsed = json.loads(output)
        assert parsed["summary"]["total_calls"] == 1
        assert "by_action" in parsed
