"""Unit tests for arq_send_email (R1 SMTP microcycle).

Mirrors the s3_cleanup transient-vs-permanent policy: permanent SMTP
rejections are swallowed (logged), transient / connection errors propagate so
ARQ's max_tries retry fires. ``send_email`` is patched — no real SMTP.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from course_supporter.workers.email_send import arq_send_email

_SEND = "course_supporter.workers.email_send.send_email"


def _ctx() -> dict[str, Any]:
    return {}


def _reset_context() -> dict[str, str]:
    return {"reset_url": "https://portal.example/t/reset?token=abc", "ttl": "30 хвилин"}


class TestHappyPath:
    @patch(_SEND, new_callable=AsyncMock)
    async def test_renders_and_sends(self, mock_send: AsyncMock) -> None:
        result = await arq_send_email(
            _ctx(),
            message_id="password_reset",
            to="student@example.com",
            context=_reset_context(),
        )
        assert result == {"sent": True}
        mock_send.assert_awaited_once()
        kwargs = mock_send.await_args.kwargs
        assert kwargs["to"] == "student@example.com"
        assert kwargs["subject"] == "Відновлення пароля"
        assert "https://portal.example/t/reset?token=abc" in kwargs["body"]


class TestPermanentSwallowed:
    """5xx and recipients-refused are permanent — return, do not raise."""

    @patch(_SEND, new_callable=AsyncMock)
    async def test_5xx_response_swallowed(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = aiosmtplib.SMTPResponseException(550, "No such user")
        result = await arq_send_email(
            _ctx(),
            message_id="password_reset",
            to="ghost@example.com",
            context=_reset_context(),
        )
        assert result["sent"] is False
        assert "550" in result["error"]

    @patch(_SEND, new_callable=AsyncMock)
    async def test_recipients_refused_swallowed(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = aiosmtplib.SMTPRecipientsRefused([])
        result = await arq_send_email(
            _ctx(),
            message_id="password_reset",
            to="ghost@example.com",
            context=_reset_context(),
        )
        assert result["sent"] is False


class TestTransientPropagates:
    """4xx and connection-level errors propagate so ARQ retries."""

    @patch(_SEND, new_callable=AsyncMock)
    async def test_4xx_response_propagates(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = aiosmtplib.SMTPResponseException(421, "Try later")
        with pytest.raises(aiosmtplib.SMTPResponseException):
            await arq_send_email(
                _ctx(),
                message_id="password_reset",
                to="s@example.com",
                context=_reset_context(),
            )

    @patch(_SEND, new_callable=AsyncMock)
    async def test_connection_error_propagates(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = aiosmtplib.SMTPServerDisconnected("gone")
        with pytest.raises(aiosmtplib.SMTPServerDisconnected):
            await arq_send_email(
                _ctx(),
                message_id="password_reset",
                to="s@example.com",
                context=_reset_context(),
            )
