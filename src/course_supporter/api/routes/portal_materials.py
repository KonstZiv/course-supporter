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
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.api.routes._portal_shared import role_visible_to_student
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

# Charset-bearing ``Content-Type`` override for text materials, keyed by
# extension. The stored object Content-Type from B2/MinIO carries no charset,
# so a UTF-8 ``.txt`` / ``.md`` rendered in the portal iframe (or opened for
# ``.md``) is decoded as a single-byte codepage -> mojibake. We override it on
# the presigned GET (``ResponseContentType``). The extension is the SAME signal
# the client uses to pick its render branch (student-path step B ratify P1: the
# extension in the signed key path); this map is its server-side mirror on the
# other side of the URL. Non-text source_types never reach this map (gated on
# ``source_type == "text"`` at the call site), so audio / video / code / slides
# stay byte-identical.
_TEXT_CHARSET_BY_EXT: Final[dict[str, str]] = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "markdown": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
}


def _text_charset_override(name: str) -> str | None:
    """Charset-bearing ``Content-Type`` for a text material by extension, else None.

    ``name`` is ``material.filename or key`` — the key preserves the original
    filename (``sanitize_s3_key``), so the extension survives even when
    ``filename`` is NULL. An unknown or missing extension → ``None`` → the
    presigned URL carries no ``ResponseContentType`` (legacy byte-identical).
    """
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _TEXT_CHARSET_BY_EXT.get(ext)


async def _resolve_enrolled_material(
    student: StudentContext,
    session: AsyncSession,
    authored_document_id: uuid.UUID,
) -> AuthoredDocument:
    """Resolve a material the session student may view, else generic 404.

    Mirrors the T2 submission gate verbatim: AuthoredDocument → course root
    (anchor) → tenant-check root → ``is_enrolled(student, course_root_id)``.
    Course-scoped access is enrollment-gated; any failure → the same 404.

    A methodological document collapses into the SAME generic 404 as a missing /
    soft-deleted / foreign material (role allowlist, :func:`role_visible_to_student`):
    the student cannot tell a methodological material apart from one that does
    not exist. No new refusal text — indistinguishability is the point (rule #12).
    """
    material = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    if (
        material is None
        or material.deleted_at is not None
        or not role_visible_to_student(material.material_role)
    ):
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
        if material.source_type == "code":
            # R3/R7 (task-code-materials): code is download-only — the
            # presigned URL is signed with an attachment disposition so
            # the browser never renders it in-page (single file and
            # archive alike). The filename rides the header when known.
            filename = (material.filename or key.rsplit("/", 1)[-1]).replace('"', "")
            url = await s3.generate_presigned_get_url(
                key,
                content_disposition=f'attachment; filename="{filename}"',
            )
            return PortalMediaResponse(kind="file", url=url)
        # Text materials (source_type == "text") get a charset-bearing
        # Content-Type so the browser reads UTF-8 instead of guessing a
        # single-byte codepage (mojibake); every other kind stays byte-identical.
        override = (
            _text_charset_override(material.filename or key)
            if material.source_type == "text"
            else None
        )
        url = await s3.generate_presigned_get_url(key, response_content_type=override)
        return PortalMediaResponse(kind="file", url=url)

    return PortalMediaResponse(kind="external", url=material.source_url)
