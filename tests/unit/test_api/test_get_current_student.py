"""Unit tests for the get_current_student bearer dependency (Phase 6 T1).

The DB-backed paths (valid token → context, revoked credential → 401) are
covered live in the portal-routes integration tests; here we cover the two
pre-DB rejection paths that need no database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.security.http import HTTPAuthorizationCredentials

from course_supporter.api.deps import get_current_student


@pytest.mark.asyncio
async def test_missing_token_returns_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_current_student(credentials=None, session=AsyncMock())
    assert exc_info.value.status_code == 401
    assert "Missing bearer token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_invalid_token_returns_401_before_db() -> None:
    """A bad token is rejected at decode — the session is never touched."""
    session = AsyncMock()
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not.a.jwt")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_student(credentials=creds, session=session)
    assert exc_info.value.status_code == 401
    assert "Invalid or expired session" in exc_info.value.detail
    session.execute.assert_not_called()
