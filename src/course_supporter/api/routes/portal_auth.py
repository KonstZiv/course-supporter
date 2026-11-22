"""Student portal session authentication endpoints (Phase 6 T1, KD17).

Routes
------
- ``POST /portal/login`` — exchange tenant-scoped login + password for a
  bearer session token.
- ``GET  /portal/me``    — the authenticated student's identity (the minimal
  protected endpoint proving a token grants access; read-path is T2).
- ``PUT  /portal/me/interface-language`` — the student picks the language of
  the portal pages (M-3); it is kept on the student's record.

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
    InterfaceLanguageRequest,
    InterfaceLanguageResponse,
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
from course_supporter.storage.student_repository import StudentRepository

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
        interface_language=(
            student.interface_language if student is not None else None
        ),
    )


@router.get("/portal/me", response_model=PortalMeResponse)
async def portal_me(student: StudentDep, session: SessionDep) -> PortalMeResponse:
    """Return the student's identity, recovery-email status and languages.

    ``preferred_language`` and ``interface_language`` come off the session
    context rather than a second read: authenticating already loaded the
    ``Student`` row. They are here rather than on their own endpoints because
    the portal fetches this once at boot: the submission form opens on the
    review language, and the pages switch to the interface language.
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
        interface_language=student.interface_language,
    )


@router.put("/portal/me/interface-language", response_model=InterfaceLanguageResponse)
async def set_interface_language(
    body: InterfaceLanguageRequest,
    student: StudentDep,
    session: SessionDep,
) -> InterfaceLanguageResponse:
    """Keep the language of the portal pages the student picked (M-3).

    Only the student of the token is changed — tenant and student both come
    from the session, never from the body. The language arrives already
    canonical (the schema validates it), and an unchanged choice costs no
    write. The review language (``preferred_language``) is not touched.
    """
    if student.interface_language != body.language:
        await StudentRepository(session).set_interface_language(
            student.tenant_id, student.student_id, body.language
        )
        await session.commit()
        logger.info(
            "student_interface_language_updated",
            student_id=str(student.student_id),
            language=body.language,
        )
    return InterfaceLanguageResponse(interface_language=body.language)
