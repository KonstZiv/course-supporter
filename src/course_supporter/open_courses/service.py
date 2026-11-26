"""Open courses (O-1): the enrollment link, signup, confirm, joining, email login.

The routes (``api/routes/enrollment_links.py``, ``api/routes/portal_join.py``)
call these functions and map their errors to HTTP; nothing here commits — the
route owns the transaction.

Signup and confirm:
    A signup keeps the visitor's form in ``portal_signups`` and creates nothing
    else (R3: the course only after the email is confirmed). Confirm creates,
    in the route's one transaction, the student, the credential with the
    confirmed email, the 'self' enrollment with its event (carrying the
    signup's ``source_tag``, Б-4), the adult mark and the consent to the terms
    (``billing.consent.record_student_consent`` — which also creates the
    student's billing account).

Logins:
    A login is unique per tenant among credentials (any state — the unique
    index does not look at ``is_active``) and among pending signups (partial
    unique index). An expired pending signup does not hold its login: it is
    dropped before a new signup or a login change takes the same login.

Login by email (variant A):
    The candidates are the active credentials with a CONFIRMED email in active
    tenants; the route checks the password against each of them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.auth.recovery_tokens import generate_token, hash_token
from course_supporter.billing.consent import record_student_consent
from course_supporter.billing.kinds import ConsentChannel
from course_supporter.open_courses.links import SIGNUP_TTL, generate_enroll_key
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    PortalSignup,
    Student,
    StudentCredential,
    StudentEnrollment,
    Tenant,
)
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

SOURCE_AUTHOR = "author"
"""``student_enrollments.source`` of a grant the author made."""
SOURCE_SELF = "self"
"""``student_enrollments.source`` of an enrollment by the course link."""


class LoginTakenError(Exception):
    """The login is held by a credential or a pending signup of the tenant.

    ``signup_id`` names the signup whose confirm met it (None elsewhere).
    """

    def __init__(self, signup_id: uuid.UUID | None = None) -> None:
        super().__init__("login taken")
        self.signup_id = signup_id


class SignupNotFoundError(Exception):
    """No pending, unexpired signup matches (unknown, used or expired)."""


class EmailConfirmationRequiredError(Exception):
    """A student without a confirmed email asked to join a course."""


# ── The enrollment link (author) ────────────────────────────────────────────


async def author_course(
    session: AsyncSession, *, tenant_id: uuid.UUID, course_id: uuid.UUID
) -> CourseNode | None:
    """The tenant's own live course (a root node), or None."""
    node = await session.get(CourseNode, course_id)
    if (
        node is None
        or node.tenant_id != tenant_id
        or node.parent_id is not None
        or node.deleted_at is not None
    ):
        return None
    return node


async def open_link(session: AsyncSession, course: CourseNode) -> bool:
    """Give the course a key if it has none; True when one was given."""
    if course.enroll_key is not None:
        return False
    course.enroll_key = generate_enroll_key()
    await session.flush()
    return True


async def rotate_link(session: AsyncSession, course: CourseNode) -> None:
    """Replace the key: the old link stops working at once."""
    course.enroll_key = generate_enroll_key()
    await session.flush()


async def close_link(session: AsyncSession, course: CourseNode) -> bool:
    """Drop the key; True when the link was open."""
    if course.enroll_key is None:
        return False
    course.enroll_key = None
    await session.flush()
    return True


# ── The course by its link (public) ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OpenCourse:
    """A course whose link is open, in an active tenant."""

    course: CourseNode
    tenant: Tenant


async def find_open_course(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> OpenCourse | None:
    """The course of ``key`` in ``tenant_id``; None when the key is unknown or
    closed, the course deleted or the tenant inactive — one answer for all."""
    row = (
        await session.execute(
            select(CourseNode, Tenant)
            .join(Tenant, CourseNode.tenant_id == Tenant.id)
            .where(
                CourseNode.enroll_key == key,
                CourseNode.tenant_id == tenant_id,
                CourseNode.parent_id.is_(None),
                CourseNode.deleted_at.is_(None),
                Tenant.is_active.is_(True),
                Tenant.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        return None
    course, tenant = row
    return OpenCourse(course=course, tenant=tenant)


async def course_languages(session: AsyncSession, course: CourseNode) -> list[str]:
    """The course language first, then the other languages its live materials
    name (ISO 639-3)."""
    found = (
        await session.scalars(
            select(distinct(AuthoredDocument.language)).where(
                AuthoredDocument.course_root_id == course.id,
                AuthoredDocument.deleted_at.is_(None),
                AuthoredDocument.language.is_not(None),
            )
        )
    ).all()
    languages = [course.default_language] if course.default_language else []
    languages += sorted(lang for lang in found if lang and lang not in languages)
    return languages


# ── Signup ──────────────────────────────────────────────────────────────────


async def login_taken(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    login: str,
    now: datetime,
    except_signup: uuid.UUID | None = None,
) -> bool:
    """True when a credential or another pending signup holds the login."""
    credential = await session.scalar(
        select(StudentCredential.id).where(
            StudentCredential.tenant_id == tenant_id,
            StudentCredential.login == login,
        )
    )
    if credential is not None:
        return True
    pending = select(PortalSignup.id).where(
        PortalSignup.tenant_id == tenant_id,
        PortalSignup.login == login,
        PortalSignup.confirmed_at.is_(None),
        PortalSignup.expires_at > now,
    )
    if except_signup is not None:
        pending = pending.where(PortalSignup.id != except_signup)
    return await session.scalar(pending.limit(1)) is not None


async def _drop_expired_holder(
    session: AsyncSession, *, tenant_id: uuid.UUID, login: str, now: datetime
) -> None:
    """Delete an expired pending signup holding ``login`` (module docstring)."""
    await session.execute(
        delete(PortalSignup).where(
            PortalSignup.tenant_id == tenant_id,
            PortalSignup.login == login,
            PortalSignup.confirmed_at.is_(None),
            PortalSignup.expires_at <= now,
        )
    )


@dataclass(frozen=True, slots=True)
class SignupForm:
    """What the visitor gave (validated by the route's schema)."""

    login: str
    password_hash: str
    email: str
    display_name: str | None
    interface_language: str | None
    terms_version: str
    text_sha256: str
    source_tag: str | None = None
    """The join link's campaign tag, already checked (``source_tag.py``)."""


async def create_signup(
    session: AsyncSession, *, open_course: OpenCourse, form: SignupForm, now: datetime
) -> tuple[PortalSignup, str]:
    """Record a signup; return it and the raw confirm token (mailed only).

    Raises:
        LoginTakenError: a credential or a pending signup holds the login.
    """
    tenant_id = open_course.tenant.id
    await _drop_expired_holder(session, tenant_id=tenant_id, login=form.login, now=now)
    if await login_taken(session, tenant_id=tenant_id, login=form.login, now=now):
        raise LoginTakenError
    raw_token = generate_token()
    signup = PortalSignup(
        tenant_id=tenant_id,
        course_node_id=open_course.course.id,
        login=form.login,
        password_hash=form.password_hash,
        email=form.email.lower(),
        display_name=form.display_name,
        interface_language=form.interface_language,
        adult_confirmed_at=now,
        terms_version=form.terms_version,
        text_sha256=form.text_sha256,
        source_tag=form.source_tag,
        token_hash=hash_token(raw_token),
        created_at=now,
        expires_at=now + SIGNUP_TTL,
    )
    try:
        async with session.begin_nested():
            session.add(signup)
            await session.flush()
    except IntegrityError as exc:  # a pending signup took the login meanwhile
        raise LoginTakenError from exc
    return signup, raw_token


async def _pending_signup(
    session: AsyncSession,
    now: datetime,
    *,
    signup_id: uuid.UUID | None = None,
    raw_token: str | None = None,
) -> PortalSignup:
    """The pending, unexpired signup by id and/or token, locked for update."""
    stmt = select(PortalSignup).where(
        PortalSignup.confirmed_at.is_(None), PortalSignup.expires_at > now
    )
    if signup_id is not None:
        stmt = stmt.where(PortalSignup.id == signup_id)
    if raw_token is not None:
        stmt = stmt.where(PortalSignup.token_hash == hash_token(raw_token))
    signup = await session.scalar(stmt.with_for_update())
    if signup is None:
        raise SignupNotFoundError
    return signup


async def reissue_token(
    session: AsyncSession, *, signup_id: uuid.UUID, now: datetime
) -> tuple[PortalSignup, str]:
    """A new confirm token for a pending signup; the old one stops working.
    The signup's expiry stays.

    Raises:
        SignupNotFoundError: no pending, unexpired signup by that id.
    """
    signup = await _pending_signup(session, now, signup_id=signup_id)
    raw_token = generate_token()
    signup.token_hash = hash_token(raw_token)
    await session.flush()
    return signup, raw_token


async def change_login(
    session: AsyncSession,
    *,
    signup_id: uuid.UUID,
    raw_token: str,
    login: str,
    now: datetime,
) -> PortalSignup:
    """Give a pending signup another login (its own was taken before confirm).

    Raises:
        SignupNotFoundError: the id and the token name no pending signup.
        LoginTakenError: the new login is held too.
    """
    signup = await _pending_signup(
        session, now, signup_id=signup_id, raw_token=raw_token
    )
    await _drop_expired_holder(
        session, tenant_id=signup.tenant_id, login=login, now=now
    )
    if await login_taken(
        session,
        tenant_id=signup.tenant_id,
        login=login,
        now=now,
        except_signup=signup.id,
    ):
        raise LoginTakenError
    try:
        async with session.begin_nested():
            signup.login = login
            await session.flush()
    except IntegrityError as exc:
        raise LoginTakenError from exc
    return signup


@dataclass(frozen=True, slots=True)
class Confirmed:
    """What a confirm created."""

    signup: PortalSignup
    student: Student


async def confirm_signup(
    session: AsyncSession, *, raw_token: str, now: datetime
) -> Confirmed:
    """Create the student, credential, enrollment, adult mark and consent of a
    pending signup (module docstring); the signup is marked confirmed.

    The course is enrolled even if its link was closed since the signup (the
    key was checked then).

    Raises:
        SignupNotFoundError: unknown, used or expired token, or the tenant is
            no longer active.
        LoginTakenError: a credential took the login since the signup; the
            signup stays (the route rolls back), the portal asks for another
            login.
    """
    signup = await _pending_signup(session, now, raw_token=raw_token)
    tenant = await session.get(Tenant, signup.tenant_id)
    if tenant is None or not tenant.is_active or tenant.deleted_at is not None:
        raise SignupNotFoundError
    if await login_taken(
        session,
        tenant_id=signup.tenant_id,
        login=signup.login,
        now=now,
        except_signup=signup.id,
    ):
        raise LoginTakenError(signup.id)

    student = Student(
        tenant_id=signup.tenant_id,
        external_id=f"portal-{uuid.uuid4().hex}",
        display_name=signup.display_name,
        interface_language=signup.interface_language,
        adult_confirmed_at=signup.adult_confirmed_at,
    )
    session.add(student)
    await session.flush()
    credential = StudentCredential(
        student_id=student.id,
        tenant_id=signup.tenant_id,
        login=signup.login,
        password_hash=signup.password_hash,
        recovery_email=signup.email,
        recovery_email_confirmed_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(credential)
            await session.flush()
    except IntegrityError as exc:  # a credential took the login meanwhile
        raise LoginTakenError(signup.id) from exc
    await StudentEnrollmentRepository(session).bind(
        tenant_id=signup.tenant_id,
        student_id=student.id,
        course_node_id=signup.course_node_id,
        source=SOURCE_SELF,
        source_tag=signup.source_tag,
    )
    # The consent is to the revision the visitor was shown at signup; if the
    # terms changed since, billing asks for the new one before a purchase.
    await record_student_consent(
        session,
        tenant_id=signup.tenant_id,
        student_id=student.id,
        terms_version=signup.terms_version,
        current_version=signup.terms_version,
        text_sha256=signup.text_sha256,
        channel=ConsentChannel.PORTAL,
    )
    signup.confirmed_at = now
    signup.student_id = student.id
    await session.flush()
    return Confirmed(signup=signup, student=student)


# ── A student with a login joins a course (R7) ──────────────────────────────


class JoinOutcome(StrEnum):
    """What joining a course did."""

    ENROLLED = "enrolled"
    ALREADY_ENROLLED = "already_enrolled"


async def join_course(
    session: AsyncSession,
    *,
    student_id: uuid.UUID,
    open_course: OpenCourse,
    terms_version: str,
    current_version: str,
    text_sha256: str,
    now: datetime,
    source_tag: str | None = None,
) -> JoinOutcome:
    """Enroll a student of the course's tenant by the link ('self').

    The adult mark and the consent are recorded when missing (a consent to the
    same revision writes nothing). The enrollment's event carries
    ``source_tag`` (the join link's campaign tag, already checked).

    Raises:
        EmailConfirmationRequiredError: the student has no confirmed email.
        BillingConflictError: ``terms_version`` is not the revision in force.
    """
    credential = await session.scalar(
        select(StudentCredential).where(StudentCredential.student_id == student_id)
    )
    if credential is None or credential.recovery_email_confirmed_at is None:
        raise EmailConfirmationRequiredError
    await record_student_consent(
        session,
        tenant_id=open_course.tenant.id,
        student_id=student_id,
        terms_version=terms_version,
        current_version=current_version,
        text_sha256=text_sha256,
        channel=ConsentChannel.PORTAL,
    )
    student = await session.get(Student, student_id)
    if student is not None and student.adult_confirmed_at is None:
        student.adult_confirmed_at = now
        await session.flush()  # outside the savepoint below: it must stay
    enrolled = await session.scalar(
        select(StudentEnrollment.id).where(
            StudentEnrollment.student_id == student_id,
            StudentEnrollment.course_node_id == open_course.course.id,
        )
    )
    if enrolled is not None:
        return JoinOutcome.ALREADY_ENROLLED
    try:
        async with session.begin_nested():
            await StudentEnrollmentRepository(session).bind(
                tenant_id=open_course.tenant.id,
                student_id=student_id,
                course_node_id=open_course.course.id,
                source=SOURCE_SELF,
                source_tag=source_tag,
            )
    except IntegrityError:  # enrolled by a parallel request
        return JoinOutcome.ALREADY_ENROLLED
    return JoinOutcome.ENROLLED


# ── Login by email (variant A) ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EmailLoginCandidate:
    """An active credential with this confirmed email in an active tenant."""

    credential: StudentCredential
    tenant: Tenant


async def email_login_candidates(
    session: AsyncSession,
    *,
    email: str,
    tenant_id: uuid.UUID | None = None,
    login: str | None = None,
) -> list[EmailLoginCandidate]:
    """The credentials a login by ``email`` may enter, narrowed to a tenant
    and/or a login when given; by tenant name, then login."""
    stmt = (
        select(StudentCredential, Tenant)
        .join(Tenant, StudentCredential.tenant_id == Tenant.id)
        .join(Student, StudentCredential.student_id == Student.id)
        .where(
            func.lower(StudentCredential.recovery_email) == email.lower(),
            StudentCredential.recovery_email_confirmed_at.is_not(None),
            StudentCredential.is_active.is_(True),
            Tenant.is_active.is_(True),
            Tenant.deleted_at.is_(None),
            Student.deleted_at.is_(None),
        )
        .order_by(Tenant.name, StudentCredential.login)
    )
    if tenant_id is not None:
        stmt = stmt.where(StudentCredential.tenant_id == tenant_id)
    if login is not None:
        stmt = stmt.where(StudentCredential.login == login)
    rows = (await session.execute(stmt)).all()
    return [EmailLoginCandidate(credential=c, tenant=t) for c, t in rows]
