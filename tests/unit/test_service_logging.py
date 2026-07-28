"""Tests for service_logging module — tenant context and DB persistence."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.service_logging import (
    _current_job_id,
    _current_tenant_id,
    get_current_job_id,
    get_current_tenant_id,
    job_scope,
    set_job_from_arq,
    set_tenant_from_job,
    tenant_scope,
)


@pytest.fixture(autouse=True)
def _reset_context() -> None:  # type: ignore[misc]
    """Ensure both ContextVars are reset to None after each test."""
    yield
    _current_tenant_id.set(None)
    _current_job_id.set(None)


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

    async def test_clears_previous_tenant_on_new_call(self) -> None:
        """Prevents tenant leakage between sequential ARQ tasks."""
        old_tid = uuid.uuid4()
        _current_tenant_id.set(old_tid)
        assert get_current_tenant_id() == old_tid

        # New task with failed lookup — must NOT inherit old tenant
        jid = uuid.uuid4()
        factory = MagicMock()
        session = AsyncMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("course_supporter.storage.job_repository.JobRepository") as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            await set_tenant_from_job(factory, jid)

        assert get_current_tenant_id() is None


class TestJobScope:
    """Test ContextVar-based job scoping (mirror of TestTenantScope)."""

    def test_default_is_none(self) -> None:
        assert get_current_job_id() is None

    def test_scope_sets_and_resets(self) -> None:
        jid = uuid.uuid4()
        with job_scope(jid):
            assert get_current_job_id() == jid
        assert get_current_job_id() is None

    def test_nested_scopes(self) -> None:
        j1, j2 = uuid.uuid4(), uuid.uuid4()
        with job_scope(j1):
            assert get_current_job_id() == j1
            with job_scope(j2):
                assert get_current_job_id() == j2
            assert get_current_job_id() == j1
        assert get_current_job_id() is None

    def test_scope_resets_on_exception(self) -> None:
        jid = uuid.uuid4()
        with pytest.raises(ValueError, match="boom"), job_scope(jid):
            assert get_current_job_id() == jid
            raise ValueError("boom")
        assert get_current_job_id() is None


class TestSetJobFromArq:
    """Test bare setter — pure ContextVar write, no DB IO."""

    def test_sets_job_id(self) -> None:
        jid = uuid.uuid4()
        set_job_from_arq(jid)
        assert get_current_job_id() == jid

    def test_overwrites_previous(self) -> None:
        """Sequential ARQ tasks reusing the same worker must not leak."""
        old, new = uuid.uuid4(), uuid.uuid4()
        set_job_from_arq(old)
        assert get_current_job_id() == old
        set_job_from_arq(new)
        assert get_current_job_id() == new


class TestTenantAndJobScopesCompose:
    """Tenant and job ContextVars are independent and compose."""

    def test_independent(self) -> None:
        tid, jid = uuid.uuid4(), uuid.uuid4()
        with tenant_scope(tid), job_scope(jid):
            assert get_current_tenant_id() == tid
            assert get_current_job_id() == jid
        assert get_current_tenant_id() is None
        assert get_current_job_id() is None

    def test_setting_one_does_not_affect_the_other(self) -> None:
        tid = uuid.uuid4()
        set_job_from_arq(uuid.uuid4())
        with tenant_scope(tid):
            assert get_current_tenant_id() == tid
        # Job ContextVar untouched by tenant scope exit
        assert get_current_job_id() is not None


class TestPersist:
    """Test _persist writes ExternalServiceCall to DB."""

    async def test_persist_writes_record(self) -> None:
        from course_supporter.service_logging import _persist

        session = AsyncMock()
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        set_job_from_arq(uuid.uuid4())
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

    async def test_persist_forwards_unit_out_reasoning(self) -> None:
        """The reasoning-token subset reaches the ExternalServiceCall row (P5/P6)."""
        from course_supporter.service_logging import _persist

        session = AsyncMock()
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        set_job_from_arq(uuid.uuid4())
        with patch("course_supporter.storage.orm.ExternalServiceCall") as mock_esc:
            await _persist(
                mock_factory,
                action="presentation_pass_1_vision",
                strategy="default",
                provider="dashscope",
                model_id="qwen3-vl-32b-instruct",
                unit_type="tokens",
                unit_out=1710,
                unit_out_reasoning=1160,
                success=True,
            )
        assert mock_esc.call_args.kwargs["unit_out_reasoning"] == 1160

    async def test_persist_swallows_db_error(self) -> None:
        from sqlalchemy.exc import OperationalError

        from course_supporter.service_logging import _persist

        session = AsyncMock()
        session.commit = AsyncMock(side_effect=OperationalError("fail", {}, None))
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        set_job_from_arq(uuid.uuid4())
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


class TestPersistGuardsMissingJobId:
    """_persist must skip + WARN when job_id ContextVar is None.

    Two-layer defense (KD5 + 0.4): ContextVar guard at the application
    layer, NOT NULL constraint at the DB layer. The skip prevents
    runtime IntegrityError on obscure paths that bypass ContextVar
    setup (e.g. legacy code, tests, future ARQ tasks that forget
    set_job_from_arq).
    """

    async def test_skips_write_with_no_job_id(self) -> None:
        from course_supporter.service_logging import _persist

        session = AsyncMock()
        factory = AsyncMock()
        factory.__aenter__ = AsyncMock(return_value=session)
        factory.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock(return_value=factory)

        # Job ContextVar deliberately left as None
        assert get_current_job_id() is None

        await _persist(
            mock_factory,
            action="ladder_attempt",
            strategy="default",
            provider="anthropic",
            model_id="claude-sonnet-4",
            success=False,
            error_message="provider 503",
        )

        # No session opened, no INSERT attempted
        mock_factory.assert_not_called()
        session.add.assert_not_called()
        session.commit.assert_not_called()

    async def test_emits_warning_on_skip(self) -> None:
        from course_supporter.service_logging import _persist

        factory = AsyncMock()
        mock_factory = MagicMock(return_value=factory)

        with patch("course_supporter.service_logging.logger") as mock_logger:
            await _persist(
                mock_factory,
                action="course_structuring",
                strategy="default",
                provider="gemini",
                model_id="gemini-2.5-flash",
            )

        mock_logger.warning.assert_called_once()
        call = mock_logger.warning.call_args
        assert call.args[0] == "esc_write_skipped_no_job_id"
        assert call.kwargs["action"] == "course_structuring"
        assert call.kwargs["provider"] == "gemini"
        assert call.kwargs["model_id"] == "gemini-2.5-flash"
