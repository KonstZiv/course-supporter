"""Student-portal media delivery (Phase 6 T3, KD17).

Route
-----
- ``GET /portal/materials/{authored_document_id}`` — issue how to render a
  material to the student: an external link, a presigned-GET URL for uploaded
  media, or the ordered presigned-GET URLs of a presentation's persisted WebP
  slides. Materials are shown originals-only; the backend never proxies media
  bytes (presigned-GET drops straight into the player/<img>).

Lives on the native session path (bearer token, ``get_current_student``): no
API key, no scope. Course-scoped access is gated by enrollment — the same
``is_enrolled`` split ratified in T2: the course root is derived from the
material's anchor, and any access failure (unknown / soft-deleted / foreign
tenant / not enrolled) collapses to a single generic 404 so the portal never
leaks which materials exist outside the student's access.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.api.schemas import PortalMediaResponse
from course_supporter.auth.context import StudentContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]

# One generic 404 for every "you cannot view this" case — unknown material,
# soft-deleted, foreign tenant, or not enrolled — so the portal never leaks
# which materials exist outside the student's access (rule #12 + P6).
_MATERIAL_NOT_FOUND = "Material not found."


async def _resolve_enrolled_material(
    student: StudentContext,
    session: AsyncSession,
    authored_document_id: uuid.UUID,
) -> AuthoredDocument:
    """Resolve a material the session student may view, else generic 404.

    Mirrors the T2 submission gate verbatim: AuthoredDocument → course root
    (anchor) → tenant-check root → ``is_enrolled(student, course_root_id)``.
    Course-scoped access is enrollment-gated; any failure → the same 404.
    """
    material = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    if material is None or material.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_MATERIAL_NOT_FOUND)

    # Tenant isolation: the material's course must be in the student's tenant.
    root_node = await CourseNodeRepository(session).get_by_id(material.course_root_id)
    if root_node is None or root_node.tenant_id != student.tenant_id:
        raise HTTPException(status_code=404, detail=_MATERIAL_NOT_FOUND)

    # Enrollment gate (Q7): the student must be enrolled in the material's course.
    enrolled = await StudentEnrollmentRepository(session).is_enrolled(
        student.student_id, material.course_root_id
    )
    if not enrolled:
        raise HTTPException(status_code=404, detail=_MATERIAL_NOT_FOUND)

    return material


@router.get(
    "/portal/materials/{authored_document_id}",
    response_model=PortalMediaResponse,
)
async def get_portal_material(
    student: StudentDep,
    session: SessionDep,
    s3: S3Dep,
    authored_document_id: Annotated[
        uuid.UUID,
        Path(description="The material (AuthoredDocument) to render."),
    ],
) -> PortalMediaResponse:
    """Issue how to render a material to the student (originals-only, KD17).

    Gated by enrollment (Q7); any access failure → generic 404. The descriptor
    discriminates on ``source_type``:

    - ``presentation`` → ``slides`` with fresh presigned-GET URLs for the
      persisted WebP renders (empty list for a pre-T3 presentation, never 500).
    - uploaded media (an S3 ``source_url``) → ``file`` with a fresh
      presigned-GET URL.
    - YouTube/web (a non-S3 ``source_url``) → ``external`` with the link.

    presigned-GET URLs are issued fresh on every call (KD17); session lifetime
    beyond the TTL → page reload re-issues (seamless refresh is DD-6-B).
    """
    material = await _resolve_enrolled_material(student, session, authored_document_id)

    if material.source_type == "presentation":
        slide_keys = material.slide_keys or []
        slide_urls = [await s3.generate_presigned_get_url(key) for key in slide_keys]
        return PortalMediaResponse(kind="slides", slide_urls=slide_urls)

    # Non-presentation: an S3 source_url is uploaded media; anything else is an
    # external link (YouTube/web) returned verbatim, never proxied through S3.
    key = s3.extract_key(material.source_url)
    if key is not None:
        url = await s3.generate_presigned_get_url(key)
        return PortalMediaResponse(kind="file", url=url)

    return PortalMediaResponse(kind="external", url=material.source_url)
