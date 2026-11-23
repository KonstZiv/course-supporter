"""Open courses in the portal (O-1): enrollment by a course link, login by email.

Routes
------
Public (no login):

- ``GET  /portal/join/{tenant_id}/{key}``        — the course the link leads to.
- ``POST /portal/join/{tenant_id}/{key}/signup`` — a signup; mails a confirm
  link (202).
- ``POST /portal/join/resend``                   — a new confirm link (202).
- ``POST /portal/join/confirm``                  — confirm: the student, the
  login and the enrollment are created; a session is returned.
- ``POST /portal/join/{signup_id}/login``        — another login for a signup
  whose login was taken before confirm (204).
- ``POST /portal/login-by-email``                — sign in by a confirmed email
  and password (variant A).

With a session:

- ``POST /portal/courses/join``                  — a student with a login joins
  a course of their author by its link (R7).

Every 4xx carries a code and no value from the request; the 422 of these
routes leaves the values out (``_join_shared``). Logs carry ids and the email
mask only (condition of Mentor 5): never an email, login, token or password.
"""

from __future__ import annotations

import functools
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_rate_limiter,
    get_session,
)
from course_supporter.api.routes._billing_shared import Refusal as BillingRefusal
from course_supporter.api.routes._billing_shared import responses as billing_responses
from course_supporter.api.routes._billing_shared import terms_changed
from course_supporter.api.routes._join_shared import (
    JoinRefusal,
    PrivateValidationRoute,
    client_ip,
    limit,
    refusal,
    responses,
)
from course_supporter.api.routes.portal_auth import INVALID_CREDENTIALS
from course_supporter.api.schemas import (
    JoinChangeLoginRequest,
    JoinConfirmRequest,
    JoinConfirmResponse,
    JoinCourseCardResponse,
    JoinResendRequest,
    JoinSignupRequest,
    JoinSignupResponse,
    PortalCourseJoinRequest,
    PortalCourseJoinResponse,
    PortalEmailLoginChoice,
    PortalEmailLoginChoiceResponse,
    PortalEmailLoginRequest,
    PortalEmailLoginResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.auth.rate_limits import RedisRateLimiter
from course_supporter.auth.student_passwords import (
    WeakPasswordError,
    hash_password,
    verify_password,
)
from course_supporter.auth.student_session import issue_session_token
from course_supporter.billing.errors import BillingConflictError
from course_supporter.config import get_settings
from course_supporter.enqueue import enqueue_email
from course_supporter.open_courses.links import (
    author_login_url,
    mask_email,
    shared_login_url,
    signup_confirm_url,
    signup_ttl_display,
)
from course_supporter.open_courses.service import (
    EmailConfirmationRequiredError,
    JoinOutcome,
    LoginTakenError,
    SignupForm,
    SignupNotFoundError,
    change_login,
    confirm_signup,
    course_languages,
    create_signup,
    email_login_candidates,
    find_open_course,
    join_course,
    reissue_token,
)
from course_supporter.storage.orm import CourseNode, PortalSignup, Student, Tenant

logger = structlog.get_logger()

router = APIRouter(tags=["portal"], route_class=PrivateValidationRoute)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
LimiterDep = Annotated[RedisRateLimiter, Depends(get_rate_limiter)]
CourseKey = Annotated[str, Path(min_length=1, max_length=200)]

SIGNUP_EMAIL = "portal_signup_confirm"
"""The ``email_text`` message id of the confirm email."""


# ── The course card ─────────────────────────────────────────────────────────


@router.get(
    "/portal/join/{tenant_id}/{key}",
    responses=responses(JoinRefusal.COURSE_NOT_FOUND),
)
async def get_join_card(
    tenant_id: uuid.UUID, key: CourseKey, session: SessionDep
) -> JoinCourseCardResponse:
    """The course the link leads to: its title, its author, its languages and
    the revision of the terms in force (the signup form agrees to it)."""
    found = await find_open_course(session, tenant_id=tenant_id, key=key)
    if found is None:
        raise refusal(JoinRefusal.COURSE_NOT_FOUND)
    return JoinCourseCardResponse(
        course_id=found.course.id,
        course_title=found.course.title,
        author_name=found.tenant.name,
        languages=await course_languages(session, found.course),
        terms_version=get_settings().billing_terms_version,
    )


# ── Signup ──────────────────────────────────────────────────────────────────


async def _mail_confirm(
    arq: ArqRedis,
    signup: PortalSignup,
    raw_token: str,
    *,
    course_title: str,
    author_name: str,
) -> None:
    """Mail the confirm link in the signup's language (Ukrainian by default).

    The signup is committed before this: if the queue does not take the mail,
    the request still stands and resend mails it again — the failure is logged,
    not answered.
    """
    language = signup.interface_language
    try:
        await enqueue_email(
            arq=arq,
            message_id=SIGNUP_EMAIL,
            to=signup.email,
            context={
                "course_title": course_title,
                "author_name": author_name,
                "confirm_url": signup_confirm_url(signup.tenant_id, raw_token),
                "ttl": signup_ttl_display(language),
                "author_login_url": author_login_url(signup.tenant_id),
                "shared_login_url": shared_login_url(),
            },
            language=language,
            recipient_masked=mask_email(signup.email),
        )
    except (RedisError, OSError) as exc:
        logger.error(
            "portal_signup_email_not_enqueued",
            signup_id=str(signup.id),
            error=type(exc).__name__,
        )


def _accepted(signup: PortalSignup) -> JoinSignupResponse:
    return JoinSignupResponse(
        signup_id=signup.id,
        email_masked=mask_email(signup.email),
        expires_at=signup.expires_at,
    )


@router.post(
    "/portal/join/{tenant_id}/{key}/signup",
    status_code=202,
    responses={
        **responses(
            JoinRefusal.COURSE_NOT_FOUND,
            JoinRefusal.LOGIN_TAKEN,
            JoinRefusal.RATE_LIMITED,
        ),
        **billing_responses(BillingRefusal.TERMS_CHANGED),
    },
)
async def post_signup(
    tenant_id: uuid.UUID,
    key: CourseKey,
    body: JoinSignupRequest,
    request: Request,
    session: SessionDep,
    arq: ArqDep,
    limiter: LimiterDep,
) -> JoinSignupResponse:
    """Ask to enroll: the request waits 7 days for the mailed link.

    Nothing but the request is created; the same email may already be on other
    logins of this author (a family, a shared mailbox) — that changes nothing,
    and the answer does not say whether it is so.
    """
    settings = get_settings()
    await limit(
        limiter,
        "join_signup_ip",
        client_ip(request),
        settings.rate_limit_join_signup_ip,
    )
    found = await find_open_course(session, tenant_id=tenant_id, key=key)
    if found is None:
        raise refusal(JoinRefusal.COURSE_NOT_FOUND)
    await limit(limiter, "join_signup_key", key, settings.rate_limit_join_signup_key)
    current = settings.billing_terms_version
    if body.terms_version != current:
        raise terms_changed(current)
    try:
        password_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    form = SignupForm(
        login=body.login,
        password_hash=password_hash,
        email=body.email,
        display_name=body.display_name,
        interface_language=body.interface_language,
        terms_version=body.terms_version,
        text_sha256=body.text_sha256,
    )
    try:
        signup, raw_token = await create_signup(
            session, open_course=found, form=form, now=datetime.now(UTC)
        )
    except LoginTakenError as exc:
        course_id = found.course.id  # read before the rollback expires it
        await session.rollback()
        logger.info(
            "portal_signup_login_taken",
            tenant_id=str(tenant_id),
            course_id=str(course_id),
        )
        raise refusal(JoinRefusal.LOGIN_TAKEN) from exc
    await session.commit()
    logger.info(
        "portal_signup_created",
        tenant_id=str(tenant_id),
        course_id=str(found.course.id),
        signup_id=str(signup.id),
        email_masked=mask_email(signup.email),
    )
    await _mail_confirm(
        arq,
        signup,
        raw_token,
        course_title=found.course.title,
        author_name=found.tenant.name,
    )
    return _accepted(signup)


@router.post(
    "/portal/join/resend",
    status_code=202,
    responses=responses(JoinRefusal.SIGNUP_NOT_FOUND, JoinRefusal.RATE_LIMITED),
)
async def post_resend(
    body: JoinResendRequest,
    request: Request,
    session: SessionDep,
    arq: ArqDep,
    limiter: LimiterDep,
) -> JoinSignupResponse:
    """A new confirm link; the previous one stops working, the expiry stays."""
    settings = get_settings()
    await limit(
        limiter,
        "join_resend_ip",
        client_ip(request),
        settings.rate_limit_join_resend_ip,
    )
    await limit(
        limiter,
        "join_resend_signup",
        str(body.signup_id),
        settings.rate_limit_join_resend_signup,
    )
    try:
        signup, raw_token = await reissue_token(
            session, signup_id=body.signup_id, now=datetime.now(UTC)
        )
    except SignupNotFoundError as exc:
        await session.rollback()
        raise refusal(JoinRefusal.SIGNUP_NOT_FOUND) from exc
    course = await session.get(CourseNode, signup.course_node_id)
    tenant = await session.get(Tenant, signup.tenant_id)
    await session.commit()
    logger.info(
        "portal_signup_resent",
        tenant_id=str(signup.tenant_id),
        course_id=str(signup.course_node_id),
        signup_id=str(signup.id),
    )
    await _mail_confirm(
        arq,
        signup,
        raw_token,
        course_title=course.title if course is not None else "",
        author_name=tenant.name if tenant is not None else "",
    )
    return _accepted(signup)


@router.post(
    "/portal/join/confirm",
    responses=responses(JoinRefusal.SIGNUP_NOT_FOUND, JoinRefusal.LOGIN_TAKEN),
)
async def post_confirm(
    body: JoinConfirmRequest, session: SessionDep
) -> JoinConfirmResponse:
    """Confirm the email: the student, the login (with this email confirmed),
    the enrollment, the adult mark and the consent — at once; then a session.

    409 ``login_taken`` (with ``signup_id``) when someone took the login since
    the signup: the request stays; send another login to
    ``POST /portal/join/{signup_id}/login`` with the same token, then confirm
    again.
    """
    try:
        done = await confirm_signup(
            session, raw_token=body.token, now=datetime.now(UTC)
        )
    except SignupNotFoundError as exc:
        await session.rollback()
        raise refusal(JoinRefusal.SIGNUP_NOT_FOUND) from exc
    except LoginTakenError as exc:
        await session.rollback()
        signup_id = str(exc.signup_id) if exc.signup_id is not None else None
        logger.info("portal_signup_login_taken_at_confirm", signup_id=signup_id)
        raise refusal(JoinRefusal.LOGIN_TAKEN, signup_id=signup_id) from exc
    await session.commit()
    signup, student = done.signup, done.student
    logger.info(
        "portal_signup_confirmed",
        tenant_id=str(signup.tenant_id),
        course_id=str(signup.course_node_id),
        signup_id=str(signup.id),
        student_id=str(student.id),
    )
    return JoinConfirmResponse(
        access_token=issue_session_token(
            student_id=student.id, tenant_id=student.tenant_id
        ),
        student_id=student.id,
        display_name=student.display_name,
        interface_language=student.interface_language,
        tenant_id=student.tenant_id,
        course_id=signup.course_node_id,
    )


@router.post(
    "/portal/join/{signup_id}/login",
    status_code=204,
    responses=responses(JoinRefusal.SIGNUP_NOT_FOUND, JoinRefusal.LOGIN_TAKEN),
)
async def post_signup_login(
    signup_id: uuid.UUID, body: JoinChangeLoginRequest, session: SessionDep
) -> None:
    """Another login for a pending request (its own was taken); the token of
    the mailed link proves the request is the caller's."""
    try:
        await change_login(
            session,
            signup_id=signup_id,
            raw_token=body.token,
            login=body.login,
            now=datetime.now(UTC),
        )
    except SignupNotFoundError as exc:
        await session.rollback()
        raise refusal(JoinRefusal.SIGNUP_NOT_FOUND) from exc
    except LoginTakenError as exc:
        await session.rollback()
        raise refusal(JoinRefusal.LOGIN_TAKEN) from exc
    await session.commit()
    logger.info("portal_signup_login_changed", signup_id=str(signup_id))


# ── A student with a login joins a course (R7) ──────────────────────────────


@router.post(
    "/portal/courses/join",
    status_code=201,
    responses={
        200: {"description": "Already enrolled: status already_enrolled."},
        **responses(
            JoinRefusal.COURSE_NOT_FOUND,
            JoinRefusal.EMAIL_CONFIRMATION_REQUIRED,
            JoinRefusal.RATE_LIMITED,
        ),
        **billing_responses(BillingRefusal.TERMS_CHANGED),
    },
)
async def post_course_join(
    body: PortalCourseJoinRequest,
    student: StudentDep,
    session: SessionDep,
    limiter: LimiterDep,
    response: Response,
) -> PortalCourseJoinResponse:
    """Join a course of the student's own author by its link, in one step.

    Needs a confirmed email (409 ``email_confirmation_required`` — the portal
    leads to adding one). The adult mark and the consent are recorded when
    missing. A link of another author is the same 404 as an unknown one: a
    session belongs to one author's portal.
    """
    settings = get_settings()
    await limit(
        limiter,
        "join_course_student",
        str(student.student_id),
        settings.rate_limit_join_course_student,
    )
    found = (
        await find_open_course(session, tenant_id=body.tenant_id, key=body.key)
        if body.tenant_id == student.tenant_id
        else None
    )
    if found is None:
        raise refusal(JoinRefusal.COURSE_NOT_FOUND)
    current = settings.billing_terms_version
    try:
        outcome = await join_course(
            session,
            student_id=student.student_id,
            open_course=found,
            terms_version=body.terms_version,
            current_version=current,
            text_sha256=body.text_sha256,
            now=datetime.now(UTC),
        )
    except EmailConfirmationRequiredError as exc:
        await session.rollback()
        raise refusal(JoinRefusal.EMAIL_CONFIRMATION_REQUIRED) from exc
    except BillingConflictError as exc:
        await session.rollback()
        raise terms_changed(current) from exc
    await session.commit()
    logger.info(
        "portal_course_joined",
        tenant_id=str(student.tenant_id),
        course_id=str(found.course.id),
        student_id=str(student.student_id),
        outcome=outcome.value,
    )
    if outcome is JoinOutcome.ALREADY_ENROLLED:
        response.status_code = 200
    return PortalCourseJoinResponse(status=outcome.value, course_id=found.course.id)


# ── Login by email (variant A) ──────────────────────────────────────────────


@functools.cache
def _decoy_hash() -> str:
    """A hash no password matches: checked when there is no candidate, so an
    unknown email takes as long as a wrong password."""
    return hash_password(secrets.token_urlsafe(32))


@router.post(
    "/portal/login-by-email",
    response_model=PortalEmailLoginResponse | PortalEmailLoginChoiceResponse,
    responses={
        401: {"description": "Invalid login or password (as POST /portal/login)."},
        **responses(JoinRefusal.RATE_LIMITED),
    },
)
async def post_login_by_email(
    body: PortalEmailLoginRequest,
    request: Request,
    session: SessionDep,
    limiter: LimiterDep,
) -> PortalEmailLoginResponse | PortalEmailLoginChoiceResponse:
    """Sign in by a confirmed email and the password.

    The password is checked against every active login with this confirmed
    email. One match: a session, as ``POST /portal/login`` gives. Several: no
    session, ``choose`` lists only the matched logins — send again with
    ``tenant_id`` and ``login`` from it, and the password is checked again for
    that login alone. None: 401 with the text of ``POST /portal/login``, the
    same whether the email is unknown or the password wrong.
    """
    settings = get_settings()
    await limit(
        limiter,
        "login_by_email_ip",
        client_ip(request),
        settings.rate_limit_login_by_email_ip,
    )
    await limit(
        limiter,
        "login_by_email_email",
        body.email,
        settings.rate_limit_login_by_email_email,
    )
    candidates = await email_login_candidates(
        session, email=body.email, tenant_id=body.tenant_id, login=body.login
    )
    if not candidates:
        verify_password(body.password, _decoy_hash())
    matched = [
        c
        for c in candidates
        if verify_password(body.password, c.credential.password_hash)
    ]
    if not matched:
        logger.info(
            "portal_login_by_email_failed",
            email_masked=mask_email(body.email),
            candidates=len(candidates),
        )
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
    if len(matched) > 1:
        logger.info(
            "portal_login_by_email_choose",
            email_masked=mask_email(body.email),
            matched=len(matched),
        )
        return PortalEmailLoginChoiceResponse(
            choose=[
                PortalEmailLoginChoice(
                    tenant_id=c.tenant.id,
                    tenant_name=c.tenant.name,
                    login=c.credential.login,
                )
                for c in matched
            ]
        )
    [chosen] = matched
    student = await session.get(Student, chosen.credential.student_id)
    logger.info(
        "portal_login_by_email_ok",
        tenant_id=str(chosen.tenant.id),
        student_id=str(chosen.credential.student_id),
    )
    return PortalEmailLoginResponse(
        access_token=issue_session_token(
            student_id=chosen.credential.student_id, tenant_id=chosen.tenant.id
        ),
        student_id=chosen.credential.student_id,
        display_name=student.display_name if student is not None else None,
        interface_language=(
            student.interface_language if student is not None else None
        ),
        tenant_id=chosen.tenant.id,
    )
