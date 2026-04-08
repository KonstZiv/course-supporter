"""Tests for SSRF protection in webhook URL validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from course_supporter.api.url_validation import validate_webhook_url


class TestValidateWebhookUrl:
    """validate_webhook_url SSRF protection tests."""

    def test_valid_https_url(self) -> None:
        """Valid HTTPS URL passes."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]
        ):
            result = validate_webhook_url("https://example.com/webhook")
        assert result == "https://example.com/webhook"

    def test_http_allowed(self) -> None:
        """HTTP URL passes (for local dev)."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]
        ):
            result = validate_webhook_url("http://example.com/webhook")
        assert result == "http://example.com/webhook"

    def test_ftp_rejected(self) -> None:
        """Non-HTTP(S) scheme rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_webhook_url("ftp://example.com/file")
        assert exc_info.value.status_code == 422
        assert "https" in str(exc_info.value.detail)

    def test_no_scheme_rejected(self) -> None:
        """URL without scheme rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_webhook_url("example.com/webhook")
        assert exc_info.value.status_code == 422

    def test_localhost_rejected(self) -> None:
        """localhost hostname rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_webhook_url("https://localhost/webhook")
        assert exc_info.value.status_code == 422
        assert "not allowed" in str(exc_info.value.detail)

    def test_metadata_endpoint_rejected(self) -> None:
        """Cloud metadata endpoint rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_webhook_url("https://metadata.google.internal/v1/")
        assert exc_info.value.status_code == 422

    def test_private_ip_127_rejected(self) -> None:
        """127.0.0.0/8 loopback rejected."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]
        ):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://internal.example.com/hook")
            assert exc_info.value.status_code == 422
            assert "private" in str(exc_info.value.detail)

    def test_private_ip_10_rejected(self) -> None:
        """10.0.0.0/8 private range rejected."""
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.1.5", 0))]):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://corp.example.com/hook")
            assert exc_info.value.status_code == 422

    def test_private_ip_172_rejected(self) -> None:
        """172.16.0.0/12 private range rejected."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("172.16.0.1", 0))]
        ):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://internal.example.com/hook")
            assert exc_info.value.status_code == 422

    def test_private_ip_192_rejected(self) -> None:
        """192.168.0.0/16 private range rejected."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 0))]
        ):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://home.example.com/hook")
            assert exc_info.value.status_code == 422

    def test_link_local_rejected(self) -> None:
        """169.254.0.0/16 link-local rejected."""
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]
        ):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://cloud.example.com/hook")
            assert exc_info.value.status_code == 422

    def test_unresolvable_hostname(self) -> None:
        """Unresolvable hostname rejected."""
        import socket

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("not found")):
            with pytest.raises(HTTPException) as exc_info:
                validate_webhook_url("https://nonexistent.invalid/hook")
            assert exc_info.value.status_code == 422
            assert "resolve" in str(exc_info.value.detail).lower()

    def test_no_hostname(self) -> None:
        """URL without hostname rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_webhook_url("https:///path")
        assert exc_info.value.status_code == 422
        assert "hostname" in str(exc_info.value.detail).lower()
