"""Student portal session authentication endpoints (Phase 6 T1, KD17).

Routes
------
- ``POST /portal/login`` — exchange tenant-scoped login + password for a
  bearer session token.
- ``GET  /portal/me``    — the authenticated student's identity (the minimal
  protected endpoint proving a token grants access; read-path is T2).

These live on the native session path: no API key, no scope. Tenant is
resolved explicitly from the request, never guessed.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.schemas import (
    PortalLoginRequest,
    PortalLoginResponse,
    PortalMeResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.auth.scopes import rate_limiter
from course_supporter.auth.student_passwords import verify_password
from course_supporter.auth.student_session import issue_session_token
from course_supporter.storage.orm import Student, Tenant
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]

# Basic brute-force throttle for the unauthenticated login endpoint (KD17
# ratify cross-cutting). The endpoint has no scope, so it is NOT covered by the
# scope rate-limit; reuse the in-memory limiter (60s window) keyed per
# (tenant, login). Residual — IP-based / rotating-login / multi-process
# brute force — is deferred (DD-6-C), same single-process limitation the scope
# limiter already carries.
_LOGIN_RATE_LIMIT = 10

# Single generic message for every auth failure — never leak whether the login
# exists, the password was wrong, or the tenant is inactive.
_INVALID_CREDENTIALS = "Invalid login or password"


@router.post("/portal/login", response_model=PortalLoginResponse)
async def portal_login(
    body: PortalLoginRequest,
    session: SessionDep,
) -> PortalLoginResponse:
    """Authenticate a student and issue a bearer session token."""
    rate_key = f"portal_login:{body.tenant_id}:{body.login}"
    allowed, retry_after = rate_limiter.check(rate_key, _LOGIN_RATE_LIMIT)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.get_active_by_tenant_login(body.tenant_id, body.login)
    if credential is None or not verify_password(
        body.password, credential.password_hash
    ):
        raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)

    # Mirror get_current_tenant: an inactive tenant cannot authenticate.
    tenant = await session.get(Tenant, body.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)

    student = await session.get(Student, credential.student_id)
    token = issue_session_token(
        student_id=credential.student_id,
        tenant_id=body.tenant_id,
    )
    logger.info(
        "portal_login_ok",
        tenant_id=str(body.tenant_id),
        student_id=str(credential.student_id),
    )
    return PortalLoginResponse(
        access_token=token,
        student_id=credential.student_id,
        display_name=student.display_name if student is not None else None,
    )


@router.get("/portal/me", response_model=PortalMeResponse)
async def portal_me(student: StudentDep, session: SessionDep) -> PortalMeResponse:
    """Return the student's identity, recovery-email status and language.

    ``preferred_language`` comes off the session context rather than a
    second read: authenticating already loaded the ``Student`` row. It is
    here rather than on its own endpoint because the portal fetches this
    once at boot and the submission form needs the value to open on the
    student's standing choice.
    """
    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.get_by_student_id(student.student_id)
    return PortalMeResponse(
        student_id=student.student_id,
        tenant_id=student.tenant_id,
        login=student.login,
        display_name=student.display_name,
        recovery_email=credential.recovery_email if credential is not None else None,
        recovery_email_confirmed=(
            credential is not None
            and credential.recovery_email_confirmed_at is not None
        ),
        preferred_language=student.preferred_language,
    )
