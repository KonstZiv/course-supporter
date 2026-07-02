"""Unit tests for the SMTP send-service (R1 SMTP microcycle).

``aiosmtplib.send`` and ``get_settings`` are both patched so the tests are
deterministic and never touch the network or ambient ``.env``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest
from pydantic import SecretStr

from course_supporter.services.email_service import send_email

_SEND = "course_supporter.services.email_service.aiosmtplib.send"
_SETTINGS = "course_supporter.services.email_service.get_settings"


def _fake_settings(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "smtp_host": "mail.test",
        "smtp_port": 2525,
        "smtp_user": "sender",
        "smtp_password": SecretStr("pw"),
        "smtp_from": "no-reply@test",
        "smtp_start_tls": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestSendEmail:
    @patch(_SEND, new_callable=AsyncMock)
    @patch(_SETTINGS)
    async def test_builds_plaintext_message(
        self, mock_settings: AsyncMock, mock_send: AsyncMock
    ) -> None:
        mock_settings.return_value = _fake_settings()
        await send_email(to="student@example.com", subject="Subj", body="Body text")

        mock_send.assert_awaited_once()
        message = mock_send.await_args.args[0]
        assert message["To"] == "student@example.com"
        assert message["From"] == "no-reply@test"
        assert message["Subject"] == "Subj"
        assert message.get_content_type() == "text/plain"
        assert message.get_content().strip() == "Body text"

    @patch(_SEND, new_callable=AsyncMock)
    @patch(_SETTINGS)
    async def test_wires_smtp_settings(
        self, mock_settings: AsyncMock, mock_send: AsyncMock
    ) -> None:
        mock_settings.return_value = _fake_settings()
        await send_email(to="s@example.com", subject="S", body="B")

        kwargs = mock_send.await_args.kwargs
        assert kwargs["hostname"] == "mail.test"
        assert kwargs["port"] == 2525
        assert kwargs["username"] == "sender"
        assert kwargs["password"] == "pw"
        assert kwargs["start_tls"] is True

    @patch(_SEND, new_callable=AsyncMock)
    @patch(_SETTINGS)
    async def test_empty_credentials_become_none(
        self, mock_settings: AsyncMock, mock_send: AsyncMock
    ) -> None:
        """Local-dev empty user/password map to None so no AUTH is attempted."""
        mock_settings.return_value = _fake_settings(
            smtp_user="", smtp_password=SecretStr(""), smtp_start_tls=False
        )
        await send_email(to="s@example.com", subject="S", body="B")

        kwargs = mock_send.await_args.kwargs
        assert kwargs["username"] is None
        assert kwargs["password"] is None
        assert kwargs["start_tls"] is False

    @patch(_SEND, new_callable=AsyncMock)
    @patch(_SETTINGS)
    async def test_propagates_smtp_failure(
        self, mock_settings: AsyncMock, mock_send: AsyncMock
    ) -> None:
        """Transport does not swallow — the caller classifies the failure."""
        mock_settings.return_value = _fake_settings()
        mock_send.side_effect = aiosmtplib.SMTPServerDisconnected("boom")
        with pytest.raises(aiosmtplib.SMTPServerDisconnected):
            await send_email(to="s@example.com", subject="S", body="B")
