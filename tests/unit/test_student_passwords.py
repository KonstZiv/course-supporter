"""Tests for student-portal password hashing (Phase 6 T1)."""

from __future__ import annotations

import pytest

from course_supporter.auth.student_passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    validate_password_length,
    verify_password,
)


class TestHashVerify:
    def test_hash_then_verify_roundtrip(self) -> None:
        h = hash_password("correct horse battery")
        assert verify_password("correct horse battery", h) is True

    def test_verify_wrong_password_false(self) -> None:
        h = hash_password("correct horse battery")
        assert verify_password("wrong password here", h) is False

    def test_verify_malformed_hash_false(self) -> None:
        """A malformed stored hash maps to False, not an exception."""
        assert verify_password("anything at all", "not-a-real-hash") is False

    def test_hash_is_salted(self) -> None:
        """Two hashes of the same password differ (random salt)."""
        assert hash_password("same password 123") != hash_password("same password 123")

    def test_hash_is_argon2id(self) -> None:
        """Produced hash uses the argon2id variant."""
        assert hash_password("argon2id please").startswith("$argon2id$")


class TestPasswordPolicy:
    def test_min_length_boundary_ok(self) -> None:
        """Exactly MIN_PASSWORD_LENGTH chars is accepted."""
        validate_password_length("x" * MIN_PASSWORD_LENGTH)  # no raise

    def test_below_min_length_raises(self) -> None:
        with pytest.raises(WeakPasswordError):
            validate_password_length("x" * (MIN_PASSWORD_LENGTH - 1))

    def test_hash_rejects_short_password(self) -> None:
        with pytest.raises(WeakPasswordError):
            hash_password("short")
