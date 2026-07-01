"""Tenant-admin student provisioning & enrollment (Phase 6 T1, KD17).

All routes require scope ``PREP`` (reuse the existing admin authorization, not
a new one). They operate strictly within the authenticated tenant.

Routes
------
- ``POST   /students``                      — provision a student + credential.
- ``GET    /students``                      — list the tenant roster (T5).
- ``POST   /students/{student_id}/revoke``  — revoke portal access.
- ``POST   /students/{student_id}/restore`` — restore portal access.
- ``POST   /students/enrollments``          — bind student↔course.
- ``DELETE /students/enrollments``          — unbind student↔course.
- ``GET    /students/{student_id}/enrollments`` — list a student's grants (T5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    EnrollmentRequest,
    ProvisionStudentRequest,
    ProvisionStudentResponse,
    StudentEnrollmentItem,
    StudentEnrollmentsResponse,
    StudentRosterItem,
    StudentRosterResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.auth.student_passwords import WeakPasswordError, hash_password
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)
from course_supporter.storage.student_repository import StudentRepository

logger = structlog.get_logger()

router = APIRouter(tags=["students"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]


@router.post("/students", status_code=201, response_model=ProvisionStudentResponse)
async def provision_student(
    body: ProvisionStudentRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> ProvisionStudentResponse:
    """Provision a student (3 external_id modes) and a portal credential.

    Student and credential are created in one transaction — a credential
    conflict rolls back the student too, so no student is left without a
    credential.
    """
    try:
        password_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    student_repo = StudentRepository(session)
    cred_repo = StudentCredentialRepository(session)

    # Resolve the student row first (existing mode validates + pre-checks).
    existing_external_id: str | None = None
    if body.mode == "existing":
        if body.student_id is None:  # guaranteed by schema; defensive
            raise HTTPException(status_code=422, detail="student_id is required.")
        student = await student_repo.get_by_id(body.student_id)
        if (
            student is None
            or student.tenant_id != tenant.tenant_id
            or student.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Student not found.")
        if await cred_repo.get_by_student_id(student.id) is not None:
            raise HTTPException(
                status_code=409, detail="Student already has a portal credential."
            )
        existing_external_id = student.external_id

    try:
        if body.mode == "existing":
            student_id = body.student_id
            external_id = existing_external_id
            if body.display_name is not None and student_id is not None:
                await student_repo.set_display_name(student_id, body.display_name)
        else:
            external_id = (
                body.external_id
                if body.mode == "manual"
                else f"portal-{uuid.uuid4().hex}"
            )
            new_student = await student_repo.create(
                tenant_id=tenant.tenant_id,
                external_id=external_id or "",
                display_name=body.display_name,
            )
            student_id = new_student.id

        if student_id is None or external_id is None:  # defensive
            raise HTTPException(status_code=500, detail="Provisioning failed.")

        await cred_repo.create(
            student_id=student_id,
            tenant_id=tenant.tenant_id,
            login=body.login,
            password_hash=password_hash,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Login or external_id already in use."
        ) from exc

    logger.info(
        "student_provisioned",
        tenant_id=str(tenant.tenant_id),
        student_id=str(student_id),
        mode=body.mode,
    )
    return ProvisionStudentResponse(
        student_id=student_id, external_id=external_id, login=body.login
    )


@router.get("/students", response_model=StudentRosterResponse)
async def list_students(
    tenant: PrepDep,
    session: SessionDep,
    limit: Annotated[
        int, Query(ge=1, le=100, description="Max students per page (1-100).")
    ] = 20,
    offset: Annotated[
        int, Query(ge=0, description="Students to skip for pagination.")
    ] = 0,
) -> StudentRosterResponse:
    """List the tenant's students with portal-access state + enrollment count.

    Includes integration-mode students without a portal credential
    (``login`` / ``is_active`` null) so the admin can attach one via the
    ``existing`` provisioning mode. Soft-deleted students are excluded;
    newest-first, paginated.
    """
    repo = StudentRepository(session)
    rows = await repo.list_with_access(tenant.tenant_id, limit=limit, offset=offset)
    total = await repo.count_for_tenant(tenant.tenant_id)
    return StudentRosterResponse(
        items=[
            StudentRosterItem(
                student_id=r.student_id,
                external_id=r.external_id,
                login=r.login,
                display_name=r.display_name,
                is_active=r.is_active,
                enrollment_count=r.enrollment_count,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _set_student_active(
    session: AsyncSession,
    tenant: TenantContext,
    student_id: uuid.UUID,
    *,
    is_active: bool,
) -> None:
    """Toggle a student's credential is_active within the tenant, or 404."""
    student = await StudentRepository(session).get_by_id(student_id)
    if student is None or student.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Student not found.")
    updated = await StudentCredentialRepository(session).set_active(
        student_id, is_active=is_active
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Student has no portal credential.")
    await session.commit()


@router.post("/students/{student_id}/revoke", status_code=204)
async def revoke_access(
    student_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Revoke a student's portal access (is_active=False). Student + history kept."""
    await _set_student_active(session, tenant, student_id, is_active=False)
    logger.info(
        "student_access_revoked",
        tenant_id=str(tenant.tenant_id),
        student_id=str(student_id),
    )


@router.post("/students/{student_id}/restore", status_code=204)
async def restore_access(
    student_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Restore a previously revoked student's portal access (is_active=True)."""
    await _set_student_active(session, tenant, student_id, is_active=True)
    logger.info(
        "student_access_restored",
        tenant_id=str(tenant.tenant_id),
        student_id=str(student_id),
    )


@router.post("/students/enrollments", status_code=201)
async def bind_enrollment(
    body: EnrollmentRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> dict[str, str]:
    """Bind a student to a course (root CourseNode). Validates root-ness."""
    student = await StudentRepository(session).get_by_id(body.student_id)
    if student is None or student.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Student not found.")

    course_node = await CourseNodeRepository(session).get_by_id(body.course_node_id)
    if course_node is None or course_node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Course not found.")
    if course_node.parent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="course_node_id must be a root node (course level).",
        )

    enroll_repo = StudentEnrollmentRepository(session)
    try:
        await enroll_repo.bind(
            student_id=body.student_id, course_node_id=body.course_node_id
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Student is already enrolled in this course."
        ) from exc

    return {
        "student_id": str(body.student_id),
        "course_node_id": str(body.course_node_id),
    }


@router.delete("/students/enrollments", status_code=204)
async def unbind_enrollment(
    tenant: PrepDep,
    session: SessionDep,
    student_id: Annotated[uuid.UUID, Query(description="Student to unenroll.")],
    course_node_id: Annotated[uuid.UUID, Query(description="Course (root node).")],
) -> None:
    """Unbind a student↔course grant (row DELETE). 404 if not bound."""
    student = await StudentRepository(session).get_by_id(student_id)
    if student is None or student.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Student not found.")

    deleted = await StudentEnrollmentRepository(session).unbind(
        student_id=student_id, course_node_id=course_node_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    await session.commit()


@router.get(
    "/students/{student_id}/enrollments",
    response_model=StudentEnrollmentsResponse,
)
async def list_student_enrollments(
    student_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> StudentEnrollmentsResponse:
    """List a student's course enrollments (oldest-first).

    Enrollments to a since-soft-deleted course are preserved (unlike the
    portal listing) so the admin can see and unbind them; the FE resolves
    each course title from the roots it already holds. 404 for a student
    outside the tenant or soft-deleted (existence not leaked).
    """
    student = await StudentRepository(session).get_by_id(student_id)
    if (
        student is None
        or student.tenant_id != tenant.tenant_id
        or student.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail="Student not found.")

    enrollments = await StudentEnrollmentRepository(session).list_for_student(
        student_id
    )
    return StudentEnrollmentsResponse(
        student_id=student_id,
        items=[
            StudentEnrollmentItem(
                course_node_id=e.course_node_id,
                enrolled_at=e.created_at,
            )
            for e in enrollments
        ],
    )
