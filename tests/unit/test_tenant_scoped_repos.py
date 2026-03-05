"""Tests for tenant-scoped repositories (PD-006)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from course_supporter.storage.repositories import (
    ExternalServiceCallRepository,
)


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestExternalServiceCallRepositoryTenantScoping:
    """Tests for tenant-scoped ExternalServiceCallRepository."""

    async def test_llm_call_repo_scoped(self) -> None:
        """get_summary() filters by tenant_id when provided."""
        session = _mock_session()
        tenant_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.one.return_value = MagicMock(
            total_calls=3,
            successful_calls=3,
            failed_calls=0,
            total_cost_usd=0.01,
            total_tokens_in=3000,
            total_tokens_out=1500,
            avg_latency_ms=300.0,
        )
        session.execute.return_value = mock_result

        repo = ExternalServiceCallRepository(session, tenant_id)
        summary = await repo.get_summary()

        assert summary.total_calls == 3
        session.execute.assert_awaited_once()

    async def test_llm_call_repo_no_tenant(self) -> None:
        """get_summary() returns all records when tenant_id is None."""
        session = _mock_session()

        mock_result = MagicMock()
        mock_result.one.return_value = MagicMock(
            total_calls=10,
            successful_calls=9,
            failed_calls=1,
            total_cost_usd=0.05,
            total_tokens_in=10000,
            total_tokens_out=5000,
            avg_latency_ms=400.0,
        )
        session.execute.return_value = mock_result

        repo = ExternalServiceCallRepository(session)
        summary = await repo.get_summary()

        assert summary.total_calls == 10
        session.execute.assert_awaited_once()
