"""The author's link to a course for self-enrollment (O-1, R1 and R4).

All routes require scope ``PREP`` and act on the tenant's own course — a root
node that is not deleted; another tenant's course, a child node or an unknown
id is the same 404.

Routes
------
- ``GET  /nodes/{course_id}/enrollment-link``        — the link and whether it
  is open.
- ``POST /nodes/{course_id}/enrollment-link/open``   — give the course a key if
  it has none (again: nothing changes).
- ``POST /nodes/{course_id}/enrollment-link/rotate`` — a new key; the old link
  stops working.
- ``POST /nodes/{course_id}/enrollment-link/close``  — no key: the link stops
  working.

None of them touches the students already enrolled. Each change is one log
line with the tenant and the course (never the key).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import EnrollmentLinkResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.open_courses.links import enrollment_url
from course_supporter.open_courses.service import (
    author_course,
    close_link,
    open_link,
    rotate_link,
)
from course_supporter.storage.orm import CourseNode

logger = structlog.get_logger()

router = APIRouter(tags=["enrollment-link"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

_NOT_FOUND = "Course not found."


async def _course(
    session: AsyncSession, tenant: TenantContext, course_id: uuid.UUID
) -> CourseNode:
    course = await author_course(
        session, tenant_id=tenant.tenant_id, course_id=course_id
    )
    if course is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return course


def _link(course: CourseNode) -> EnrollmentLinkResponse:
    key = course.enroll_key
    return EnrollmentLinkResponse(
        open=key is not None,
        key=key,
        url=enrollment_url(course.tenant_id, key) if key is not None else None,
    )


def _log(event: str, course: CourseNode) -> None:
    logger.info(event, tenant_id=str(course.tenant_id), course_id=str(course.id))


@router.get("/nodes/{course_id}/enrollment-link")
async def get_enrollment_link(
    course_id: uuid.UUID, tenant: PrepDep, session: SessionDep
) -> EnrollmentLinkResponse:
    """The course's self-enrollment link and whether it is open."""
    return _link(await _course(session, tenant, course_id))


@router.post("/nodes/{course_id}/enrollment-link/open")
async def open_enrollment_link(
    course_id: uuid.UUID, tenant: PrepDep, session: SessionDep
) -> EnrollmentLinkResponse:
    """Open the link: a key is given if the course has none."""
    course = await _course(session, tenant, course_id)
    if await open_link(session, course):
        await session.commit()
        _log("course_enrollment_link_opened", course)
    return _link(course)


@router.post("/nodes/{course_id}/enrollment-link/rotate")
async def rotate_enrollment_link(
    course_id: uuid.UUID, tenant: PrepDep, session: SessionDep
) -> EnrollmentLinkResponse:
    """A new key: the old link answers 404 from now on."""
    course = await _course(session, tenant, course_id)
    await rotate_link(session, course)
    await session.commit()
    _log("course_enrollment_link_rotated", course)
    return _link(course)


@router.post("/nodes/{course_id}/enrollment-link/close")
async def close_enrollment_link(
    course_id: uuid.UUID, tenant: PrepDep, session: SessionDep
) -> EnrollmentLinkResponse:
    """Close the link; the students already enrolled keep the course."""
    course = await _course(session, tenant, course_id)
    if await close_link(session, course):
        await session.commit()
        _log("course_enrollment_link_closed", course)
    return _link(course)
