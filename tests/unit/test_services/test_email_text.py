"""Unit tests for the transactional email-template registry (R1/M-7)."""

from __future__ import annotations

import re

import pytest

from course_supporter.services.email_text import _TEMPLATES, render_email


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


class TestRenderEnglish:
    def test_password_reset_english(self) -> None:
        rendered = render_email(
            "password_reset",
            language="eng",
            reset_url="https://portal.example/t/reset?token=abc",
            ttl="30 minutes",
        )
        assert rendered.subject == "Password reset"
        assert "https://portal.example/t/reset?token=abc" in rendered.body
        assert "30 minutes" in rendered.body
        assert rendered.body.endswith("\n")

    def test_email_confirm_english(self) -> None:
        rendered = render_email(
            "email_confirm",
            language="eng",
            confirm_url="https://portal.example/t/confirm?token=xyz",
            ttl="24 hours",
        )
        assert rendered.subject == "Confirm your address for password recovery"
        assert "https://portal.example/t/confirm?token=xyz" in rendered.body
        assert "24 hours" in rendered.body
        assert rendered.body.endswith("\n")

    def test_missing_placeholder_in_english_raises(self) -> None:
        with pytest.raises(KeyError):
            render_email("password_reset", language="eng")
        with pytest.raises(KeyError):
            render_email("email_confirm", language="eng")


class TestRenderLanguageFallback:
    def test_none_language_defaults_to_ukrainian(self) -> None:
        rendered = render_email(
            "password_reset",
            language=None,
            reset_url="https://portal.example/t/reset",
            ttl="30 хвилин",
        )
        assert rendered.subject == "Відновлення пароля"

    def test_ukr_language_renders_ukrainian(self) -> None:
        rendered = render_email(
            "password_reset",
            language="ukr",
            reset_url="https://portal.example/t/reset",
            ttl="30 хвилин",
        )
        assert rendered.subject == "Відновлення пароля"

    def test_unsupported_language_falls_back_to_ukrainian(self) -> None:
        rendered = render_email(
            "password_reset",
            language="deu",
            reset_url="https://portal.example/t/reset",
            ttl="30 хвилин",
        )
        assert rendered.subject == "Відновлення пароля"


class TestEnglishTemplatesNoCyrillic:
    def test_no_cyrillic_in_english_templates(self) -> None:
        cyrillic = re.compile(r"[\u0400-\u04FF]")
        for msg_id, tmpl in _TEMPLATES["eng"].items():
            assert not cyrillic.search(tmpl.subject), (
                f"Cyrillic in English {msg_id} subject: {tmpl.subject!r}"
            )
            assert not cyrillic.search(tmpl.body), (
                f"Cyrillic in English {msg_id} body: {tmpl.body!r}"
            )


class TestRenderErrors:
    def test_unknown_message_id_raises(self) -> None:
        with pytest.raises(KeyError):
            render_email("does_not_exist", reset_url="x", ttl="y")

    def test_missing_placeholder_raises(self) -> None:
        """A body placeholder with no supplied value is a programmer error."""
        with pytest.raises(KeyError):
            render_email("password_reset")
