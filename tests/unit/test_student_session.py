"""Tests for student-portal session tokens (Phase 6 T1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from course_supporter.auth.student_session import (
    SessionTokenError,
    decode_session_token,
    issue_session_token,
)
from course_supporter.config import get_settings


class TestIssueDecode:
    def test_roundtrip_returns_ids(self) -> None:
        sid = uuid.uuid4()
        tid = uuid.uuid4()
        token = issue_session_token(student_id=sid, tenant_id=tid)
        got_sid, got_tid = decode_session_token(token)
        assert got_sid == sid
        assert got_tid == tid

    def test_expired_token_raises(self) -> None:
        """A token issued far enough in the past is expired (TTL elapsed)."""
        past = datetime.now(UTC) - timedelta(
            hours=get_settings().portal_session_ttl_hours + 1
        )
        token = issue_session_token(
            student_id=uuid.uuid4(), tenant_id=uuid.uuid4(), now=past
        )
        with pytest.raises(SessionTokenError):
            decode_session_token(token)

    def test_wrong_secret_raises(self) -> None:
        """A token signed with a different secret fails verification."""
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "tid": str(uuid.uuid4()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "a-different-secret-of-sufficient-length-32+",
            algorithm="HS256",
        )
        with pytest.raises(SessionTokenError):
            decode_session_token(forged)

    def test_garbage_token_raises(self) -> None:
        with pytest.raises(SessionTokenError):
            decode_session_token("not.a.jwt")

    def test_malformed_payload_raises(self) -> None:
        """A validly-signed token missing 'sub' is rejected as malformed."""
        secret = get_settings().portal_session_secret.get_secret_value()
        token = jwt.encode(
            {
                "tid": str(uuid.uuid4()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            secret,
            algorithm="HS256",
        )
        with pytest.raises(SessionTokenError):
            decode_session_token(token)
