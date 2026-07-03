"""Tests for recovery-token constants and helpers (Phase 6 R2)."""

from __future__ import annotations

from course_supporter.auth.keys import hash_api_key
from course_supporter.auth.recovery_tokens import (
    EMAIL_CONFIRM_TTL,
    PASSWORD_RESET_TTL,
    PURPOSE_EMAIL_CONFIRM,
    PURPOSE_PASSWORD_RESET,
    generate_token,
    hash_token,
    ttl_display,
    ttl_for,
)


class TestGenerateToken:
    def test_tokens_are_unique(self) -> None:
        """Two generated tokens differ (fresh randomness each call)."""
        assert generate_token() != generate_token()

    def test_token_is_urlsafe_nonempty(self) -> None:
        token = generate_token()
        assert token
        # token_urlsafe uses only URL-safe base64 alphabet.
        assert all(c.isalnum() or c in "-_" for c in token)


class TestHashToken:
    def test_hash_is_sha256_of_raw(self) -> None:
        """hash_token is the SHA-256 API-key hasher (64-char hex)."""
        raw = generate_token()
        h = hash_token(raw)
        assert h == hash_api_key(raw)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self) -> None:
        """Same raw token hashes identically (needed for redemption lookup)."""
        raw = generate_token()
        assert hash_token(raw) == hash_token(raw)

    def test_hash_fits_token_hash_column(self) -> None:
        """SHA-256 hex is always 64 chars — matches token_hash String(64)."""
        assert len(hash_token(generate_token())) == 64


class TestTtl:
    def test_reset_ttl_short(self) -> None:
        assert PASSWORD_RESET_TTL.total_seconds() == 30 * 60
        assert ttl_for(PURPOSE_PASSWORD_RESET) == PASSWORD_RESET_TTL

    def test_confirm_ttl_longer(self) -> None:
        assert EMAIL_CONFIRM_TTL.total_seconds() == 24 * 60 * 60
        assert ttl_for(PURPOSE_EMAIL_CONFIRM) == EMAIL_CONFIRM_TTL

    def test_reset_shorter_than_confirm(self) -> None:
        assert PASSWORD_RESET_TTL < EMAIL_CONFIRM_TTL

    def test_ttl_display_matches_purposes(self) -> None:
        assert ttl_display(PURPOSE_PASSWORD_RESET) == "30 хвилин"
        assert ttl_display(PURPOSE_EMAIL_CONFIRM) == "24 години"
