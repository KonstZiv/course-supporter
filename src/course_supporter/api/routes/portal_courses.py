"""Student-portal materials-listing (Phase 6 T4a, KD17).

Routes
------
- ``GET /portal/courses`` — the student's enrolled courses (id + title only).

Closes the T4a gap: T3 built only ``GET /portal/materials/{id}`` (a URL for a
KNOWN material id); there was no way for a portal session to enumerate courses
or materials. These listing heads add the "courses → course → material tree"
navigation surface.

Like the other portal routes these live on the native session path (bearer
token, ``get_current_student``): no API key, no scope. Visibility follows
publish-gate A (KD17): enrollment IS the publish gate — a student sees exactly
the courses they are enrolled in; the author controls visibility by WHEN they
enroll a student, not by a flag on the node (CourseNode carries no publish
column). Course-scoped access failures collapse to a single generic 404
(rule #12) so the portal never leaks which courses/materials exist outside the
student's access.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.schemas import PortalCourseListItem
from course_supporter.auth.context import StudentContext
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]


@router.get("/portal/courses", response_model=list[PortalCourseListItem])
async def list_portal_courses(
    student: StudentDep,
    session: SessionDep,
) -> list[PortalCourseListItem]:
    """List the student's enrolled courses — bare (id + title only).

    The landing screen is deliberately cheap: no counts, no peek into the tree
    (that is the per-course materials endpoint). Enrollment IS the visibility
    gate (publish-gate A): the list is exactly the student's active enrollments;
    soft-deleted course roots are filtered out. An un-enrolled student gets an
    empty list — never an error (there is nothing whose existence to leak).
    """
    courses = await StudentEnrollmentRepository(session).list_enrolled_courses(
        student.student_id
    )
    return [
        PortalCourseListItem(id=course.id, title=course.title) for course in courses
    ]
