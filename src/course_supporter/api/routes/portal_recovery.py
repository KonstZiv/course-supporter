"""Student portal password-recovery endpoints (Phase 6 R2).

Routes
------
- ``POST /portal/recovery-email``          — set / change the recovery email
  (protected: student session). Resets confirmation, burns unused confirm
  tokens, issues a fresh one, and mails a confirm link.
- ``POST /portal/recovery-email/confirm``  — confirm the recovery email from
  the mailed token (public).
- ``POST /portal/password/forgot``         — request a reset link (public).
  Always returns a generic 202; a link is only sent to a CONFIRMED recovery
  address of an ACTIVE credential (anti-enumeration).
- ``POST /portal/password/reset``          — set a new password from the mailed
  token (public).

Two-tier model: access (``is_active`` / enrollment) is the tenant's territory,
the secret (password) is the student's. A reset rotates only the secret and
only for an active credential — it never revives revoked access. The redemption
gate live-checks ``is_active`` for BOTH purposes (mirror of get_current_student).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_session,
)
from course_supporter.api.schemas import (
    ConfirmRecoveryEmailRequest,
    ForgotPasswordRequest,
    RecoveryEmailRequest,
    RecoveryEmailResponse,
    ResetPasswordRequest,
)
from course_supporter.auth.context import StudentContext
from course_supporter.auth.recovery_tokens import (
    PURPOSE_EMAIL_CONFIRM,
    PURPOSE_PASSWORD_RESET,
    generate_token,
    hash_token,
    ttl_display,
    ttl_for,
)
from course_supporter.auth.scopes import rate_limiter
from course_supporter.auth.student_passwords import WeakPasswordError, hash_password
from course_supporter.config import get_settings
from course_supporter.enqueue import enqueue_email
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)
from course_supporter.storage.student_credential_token_repository import (
    StudentCredentialTokenRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

# forgot-password throttle — reuse the login limiter (60s window), keyed per
# (tenant, login). Lower than login (10): forgot triggers an email, so the spam
# surface is larger. Residual (IP / rotating-login / multi-process) — DD-6-C.
_FORGOT_RATE_LIMIT = 3

# One generic message for every token failure — never leak whether a token is
# unknown, already used, expired, or belongs to a revoked credential.
_INVALID_TOKEN = "Invalid or expired token"  # noqa: S105 — user-facing message, not a secret


def _recovery_link(*, tenant_id: uuid.UUID, route: str, raw_token: str) -> str:
    """Build a portal recovery link ``{base}/{tenant_id}/{route}?token=...``.

    Mirrors the DD-6-M portal login link shape; the token is URL-safe
    (``secrets.token_urlsafe``) so it needs no escaping.
    """
    base = get_settings().portal_base_url.rstrip("/")
    return f"{base}/{tenant_id}/{route}?token={raw_token}"


@router.post("/portal/recovery-email", response_model=RecoveryEmailResponse)
async def set_recovery_email(
    body: RecoveryEmailRequest,
    student: StudentDep,
    session: SessionDep,
    arq: ArqDep,
) -> RecoveryEmailResponse:
    """Set / change the student's recovery email and mail a confirm link.

    Changing the address resets its confirmed state and burns any outstanding
    confirm token, so only the newest link works. The student is authenticated,
    so a missing credential is not expected — treated as 404 defensively.
    """
    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.set_recovery_email(
        student.student_id, recovery_email=body.email
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="No credential for this student")

    raw_token = generate_token()
    expires_at = datetime.now(UTC) + ttl_for(PURPOSE_EMAIL_CONFIRM)
    token_repo = StudentCredentialTokenRepository(session)
    await token_repo.issue(
        credential_id=credential.id,
        purpose=PURPOSE_EMAIL_CONFIRM,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    # Commit the token before the mail side-effect so the redemption target
    # exists when the student clicks the link.
    await session.commit()

    confirm_url = _recovery_link(
        tenant_id=student.tenant_id,
        route="confirm-email",
        raw_token=raw_token,
    )
    await enqueue_email(
        arq=arq,
        message_id=PURPOSE_EMAIL_CONFIRM,
        to=body.email,
        context={"confirm_url": confirm_url, "ttl": ttl_display(PURPOSE_EMAIL_CONFIRM)},
    )
    logger.info("portal_recovery_email_set", student_id=str(student.student_id))
    return RecoveryEmailResponse(
        recovery_email=body.email,
        recovery_email_confirmed=False,
    )


@router.post(
    "/portal/recovery-email/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def confirm_recovery_email(
    body: ConfirmRecoveryEmailRequest,
    session: SessionDep,
) -> None:
    """Confirm the recovery email from the mailed token (public)."""
    token_repo = StudentCredentialTokenRepository(session)
    credential_id = await token_repo.redeem(
        token_hash=hash_token(body.token),
        purpose=PURPOSE_EMAIL_CONFIRM,
    )
    if credential_id is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)

    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.get_by_id(credential_id)
    # Redemption gate: a revoked credential cannot confirm (same generic error).
    if credential is None or not credential.is_active:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)

    await cred_repo.confirm_recovery_email(credential_id)
    await session.commit()
    logger.info("portal_recovery_email_confirmed", credential_id=str(credential_id))


@router.post(
    "/portal/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
)
async def forgot_password(
    body: ForgotPasswordRequest,
    session: SessionDep,
    arq: ArqDep,
) -> None:
    """Request a password-reset link (public, anti-enumeration).

    Always returns a generic 202. A link is sent only when the credential is
    active AND has a CONFIRMED recovery email; an unknown login, a pending or
    absent recovery address, or a revoked credential all take the same silent
    202 path — an unverified address is never a channel for a reset token.
    """
    rate_key = f"portal_forgot:{body.tenant_id}:{body.login}"
    allowed, retry_after = rate_limiter.check(rate_key, _FORGOT_RATE_LIMIT)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )

    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.get_active_by_tenant_login(body.tenant_id, body.login)
    if (
        credential is None
        or credential.recovery_email is None
        or credential.recovery_email_confirmed_at is None
    ):
        # No eligible confirmed recovery address — silent generic 202.
        return None

    raw_token = generate_token()
    expires_at = datetime.now(UTC) + ttl_for(PURPOSE_PASSWORD_RESET)
    token_repo = StudentCredentialTokenRepository(session)
    await token_repo.issue(
        credential_id=credential.id,
        purpose=PURPOSE_PASSWORD_RESET,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    await session.commit()

    reset_url = _recovery_link(
        tenant_id=body.tenant_id,
        route="reset-password",
        raw_token=raw_token,
    )
    await enqueue_email(
        arq=arq,
        message_id=PURPOSE_PASSWORD_RESET,
        to=credential.recovery_email,
        context={"reset_url": reset_url, "ttl": ttl_display(PURPOSE_PASSWORD_RESET)},
    )
    logger.info("portal_forgot_sent", credential_id=str(credential.id))
    return None


@router.post(
    "/portal/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reset_password(
    body: ResetPasswordRequest,
    session: SessionDep,
) -> None:
    """Set a new password from the mailed reset token (public).

    The new password is validated + hashed BEFORE the token is redeemed, so a
    too-short password (422) does not burn the token. Redemption is single-use;
    the credential is then live-checked for ``is_active`` — a reset rotates only
    the secret and never revives a revoked credential.
    """
    try:
        password_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    token_repo = StudentCredentialTokenRepository(session)
    credential_id = await token_repo.redeem(
        token_hash=hash_token(body.token),
        purpose=PURPOSE_PASSWORD_RESET,
    )
    if credential_id is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)

    cred_repo = StudentCredentialRepository(session)
    credential = await cred_repo.get_by_id(credential_id)
    if credential is None or not credential.is_active:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN)

    await cred_repo.set_password(credential.student_id, password_hash=password_hash)
    await session.commit()
    logger.info("portal_password_reset", student_id=str(credential.student_id))
