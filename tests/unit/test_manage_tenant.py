"""Tests for tenant management CLI (PD-007)."""

from __future__ import annotations

import argparse
import uuid
from unittest.mock import MagicMock, patch

import pytest
from scripts.manage_tenant import (
    create_key,
    create_tenant,
    list_tenants,
    revoke_key,
)

from course_supporter.storage.orm import APIKey, Tenant


@pytest.fixture()
def mock_session() -> MagicMock:
    """Create a mock sync Session."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


@pytest.fixture()
def _patch_session(mock_session: MagicMock) -> MagicMock:
    """Patch get_sync_session to return mock."""
    with patch("scripts.manage_tenant.get_sync_session", return_value=mock_session):
        yield mock_session


class TestCreateTenant:
    def test_create_tenant(
        self,
        _patch_session: MagicMock,
        mock_session: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """create-tenant creates tenant with correct name."""
        # No existing tenant
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        args = argparse.Namespace(name="Python Academy")
        create_tenant(args)

        mock_session.add.assert_called_once()
        tenant: Tenant = mock_session.add.call_args[0][0]
        assert isinstance(tenant, Tenant)
        assert tenant.name == "Python Academy"
        mock_session.commit.assert_called_once()

        captured = capsys.readouterr()
        assert "Tenant created: Python Academy" in captured.out


class TestCreateKey:
    def test_create_key_outputs_full_key(
        self,
        _patch_session: MagicMock,
        mock_session: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """create-key outputs the full key for saving."""

        tenant = MagicMock(spec=Tenant)
        tenant.id = uuid.uuid4()
        tenant.name = "Test Tenant"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        args = argparse.Namespace(
            tenant="Test Tenant",
            scopes="prep,check",
            label="production",
            rate_prep=60,
            rate_check=300,
        )

        with patch("scripts.manage_tenant.generate_api_key") as mock_gen:
            mock_gen.return_value = ("cs_live_full123", "hash123", "cs_live_full")
            create_key(args)

        captured = capsys.readouterr()
        assert "cs_live_full123" in captured.out
        assert "cs_live_full" in captured.out
        assert "prep, check" in captured.out
        assert "production" in captured.out
        assert "Save this key" in captured.out

        mock_session.add.assert_called_once()
        api_key: APIKey = mock_session.add.call_args[0][0]
        assert isinstance(api_key, APIKey)
        assert api_key.scopes == ["prep", "check"]

    def test_create_key_unknown_tenant(
        self, _patch_session: MagicMock, mock_session: MagicMock
    ) -> None:
        """create-key exits with error for unknown tenant."""

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        args = argparse.Namespace(
            tenant="NonExistent",
            scopes="prep",
            label="default",
            rate_prep=60,
            rate_check=300,
        )

        with pytest.raises(SystemExit) as exc_info:
            create_key(args)
        assert exc_info.value.code == 1


class TestRevokeKey:
    def test_revoke_key(
        self,
        _patch_session: MagicMock,
        mock_session: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """revoke-key deactivates key by prefix."""

        key = MagicMock(spec=APIKey)
        key.key_prefix = "cs_live_a1b2"
        key.is_active = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_session.execute.return_value = mock_result

        args = argparse.Namespace(prefix="cs_live_a1b2")
        revoke_key(args)

        assert key.is_active is False
        mock_session.commit.assert_called_once()

        captured = capsys.readouterr()
        assert "Key revoked: cs_live_a1b2" in captured.out


class TestListTenants:
    def test_list_tenants(
        self,
        _patch_session: MagicMock,
        mock_session: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """list-tenants displays tenants with key counts."""

        rows = [
            MagicMock(is_active=True, key_count=1, **{"name": "DevOps School"}),
            MagicMock(is_active=True, key_count=2, **{"name": "Python Academy"}),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = rows
        mock_session.execute.return_value = mock_result

        args = argparse.Namespace()
        list_tenants(args)

        captured = capsys.readouterr()
        assert "DevOps School" in captured.out
        assert "Python Academy" in captured.out
        assert "2 keys" in captured.out
        assert "1 key)" in captured.out  # singular
