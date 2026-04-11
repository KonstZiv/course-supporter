"""Tests for service_logging module — tenant context and DB persistence."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.service_logging import (
    _current_tenant_id,
    get_current_tenant_id,
    set_tenant_from_job,
    tenant_scope,
)


@pytest.fixture(autouse=True)
def _reset_tenant_context() -> None:  # type: ignore[misc]
    """Ensure ContextVar is reset to None after each test."""
    yield
    _current_tenant_id.set(None)


class TestTenantScope:
    """Test ContextVar-based tenant scoping."""

    def test_default_is_none(self) -> None:
        assert get_current_tenant_id() is None

    def test_scope_sets_and_resets(self) -> None:
        tid = uuid.uuid4()
        with tenant_scope(tid):
            assert get_current_tenant_id() == tid
        assert get_current_tenant_id() is None

    def test_nested_scopes(self) -> None:
        t1, t2 = uuid.uuid4(), uuid.uuid4()
        with tenant_scope(t1):
            assert get_current_tenant_id() == t1
            with tenant_scope(t2):
                assert get_current_tenant_id() == t2
            assert get_current_tenant_id() == t1
        assert get_current_tenant_id() is None

    def test_scope_resets_on_exception(self) -> None:
        tid = uuid.uuid4()
        with pytest.raises(ValueError, match="boom"), tenant_scope(tid):
            assert get_current_tenant_id() == tid
            raise ValueError("boom")
        assert get_current_tenant_id() is None


class TestSetTenantFromJob:
    """Test async tenant lookup from Job."""

    async def test_sets_tenant_id_from_job(self) -> None:
        tid = uuid.uuid4()
        jid = uuid.uuid4()
        mock_job = MagicMock(tenant_id=tid)

        factory = MagicMock()
        session = AsyncMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("course_supporter.storage.job_repository.JobRepository") as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=mock_job)
            await set_tenant_from_job(factory, jid)

        assert get_current_tenant_id() == tid

    async def test_no_job_found(self) -> None:
        jid = uuid.uuid4()
        factory = MagicMock()
        session = AsyncMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("course_supporter.storage.job_repository.JobRepository") as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            await set_tenant_from_job(factory, jid)

        assert get_current_tenant_id() is None

    async def test_db_error_swallowed(self) -> None:
        """SQLAlchemy errors are caught and logged, not propagated."""
        from sqlalchemy.exc import OperationalError

        jid = uuid.uuid4()
        factory = MagicMock()
        session = AsyncMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("course_supporter.storage.job_repository.JobRepository") as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(
                side_effect=OperationalError("conn lost", {}, None)
            )
            await set_tenant_from_job(factory, jid)

        assert get_current_tenant_id() is None

    async def test_type_error_swallowed(self) -> None:
        """TypeError from mock session_factory is handled gracefully."""
        jid = uuid.uuid4()
        factory = MagicMock()  # no async context manager setup

        await set_tenant_from_job(factory, jid)  # should not raise

        assert get_current_tenant_id() is None


class TestPersist:
    """Test _persist writes ExternalServiceCall to DB."""

    async def test_persist_writes_record(self) -> None:
        from course_supporter.service_logging import _persist

        session = AsyncMock()
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        with patch("course_supporter.storage.orm.ExternalServiceCall"):
            await _persist(
                mock_factory,
                action="test_action",
                strategy="default",
                provider="gemini",
                model_id="gemini-2.5-flash",
                success=True,
            )

        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    async def test_persist_swallows_db_error(self) -> None:
        from sqlalchemy.exc import OperationalError

        from course_supporter.service_logging import _persist

        session = AsyncMock()
        session.commit = AsyncMock(side_effect=OperationalError("fail", {}, None))
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        with patch("course_supporter.storage.orm.ExternalServiceCall"):
            await _persist(
                mock_factory,
                action="test",
                strategy="default",
                provider="test",
                model_id="test",
                success=True,
            )
        # Should not raise
