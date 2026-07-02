"""Unit tests for the uk email-template registry (R1 SMTP microcycle)."""

from __future__ import annotations

import pytest

from course_supporter.services.email_text import render_email


class TestRenderPasswordReset:
    def test_fills_placeholders(self) -> None:
        rendered = render_email(
            "password_reset",
            reset_url="https://portal.example/t/reset?token=abc",
            ttl="30 хвилин",
        )
        assert rendered.subject == "Відновлення пароля"
        assert "https://portal.example/t/reset?token=abc" in rendered.body
        assert "30 хвилин" in rendered.body

    def test_no_placeholder_braces_leak(self) -> None:
        rendered = render_email("password_reset", reset_url="URL", ttl="TTL")
        assert "{reset_url}" not in rendered.body
        assert "{ttl}" not in rendered.body


class TestRenderEmailConfirm:
    def test_fills_placeholders(self) -> None:
        rendered = render_email(
            "email_confirm",
            confirm_url="https://portal.example/t/confirm?token=xyz",
            ttl="24 години",
        )
        assert rendered.subject.startswith("Підтвердження")
        assert "https://portal.example/t/confirm?token=xyz" in rendered.body
        assert "{confirm_url}" not in rendered.body


class TestRenderErrors:
    def test_unknown_message_id_raises(self) -> None:
        with pytest.raises(KeyError):
            render_email("does_not_exist", reset_url="x", ttl="y")

    def test_missing_placeholder_raises(self) -> None:
        """A body placeholder with no supplied value is a programmer error."""
        with pytest.raises(KeyError):
            render_email("password_reset")
