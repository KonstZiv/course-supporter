"""Course management API endpoints."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    CourseCreateRequest,
    CourseDetailResponse,
    CourseListResponse,
    CourseResponse,
    LessonDetailResponse,
    MaterialCreateResponse,
    NodeWithMaterialsResponse,
    SlideVideoMapItemResponse,
    SlideVideoMapRequest,
    SlideVideoMapResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_ingestion
from course_supporter.models.source import SourceType
from course_supporter.storage.mapping_validation import (
    MappingValidationError,
    MappingValidationService,
)
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MappingValidationState
from course_supporter.storage.repositories import (
    CourseRepository,
    LessonRepository,
    SlideVideoMappingRepository,
    SourceMaterialRepository,
)
from course_supporter.storage.s3 import S3Client, upload_file_chunks

logger = structlog.get_logger()

router = APIRouter(tags=["courses"])


# Allowed file extensions per source_type (lowercase, with dot).
# web does not accept file uploads at all.
def _file_extension(filename: str | None) -> str:
    """Extract lowercase file extension from filename."""
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", maxsplit=1)[-1].lower()


def _ve_to_dict(err: MappingValidationError) -> dict[str, str | None]:
    """Convert MappingValidationError dataclass to a JSON-safe dict."""
    return asdict(err)


ALLOWED_EXTENSIONS: dict[SourceType, frozenset[str]] = {
    SourceType.VIDEO: frozenset({".mp4", ".webm", ".mkv", ".avi"}),
    SourceType.PRESENTATION: frozenset({".pdf", ".pptx"}),
    SourceType.TEXT: frozenset(
        {".md", ".markdown", ".docx", ".html", ".htm", ".txt"},
    ),
}

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]
SharedDep = Annotated[TenantContext, Depends(require_scope("prep", "check"))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> CourseResponse:
    """Create a new course."""
    repo = CourseRepository(session, tenant.tenant_id)
    course = await repo.create(title=body.title, description=body.description)
    await session.commit()
    return CourseResponse.model_validate(course)


@router.get("/courses")
async def list_courses(
    tenant: SharedDep,
    session: SessionDep,
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of courses to return (1-100).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of courses to skip for pagination.",
    ),
) -> CourseListResponse:
    """List courses for the authenticated tenant.

    Returns a paginated list of courses sorted by creation date
    (newest first). Use ``limit`` and ``offset`` to page through
    results. The ``total`` field indicates the overall count.

    Args:
        limit: Maximum number of courses per page (1-100, default 20).
        offset: Number of courses to skip for pagination (default 0).
    """
    repo = CourseRepository(session, tenant.tenant_id)
    courses = await repo.list_all(limit=limit, offset=offset)
    total = await repo.count()
    return CourseListResponse(
        items=[CourseResponse.model_validate(c) for c in courses],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/courses/{course_id}")
async def get_course(
    course_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> CourseDetailResponse:
    """Get course by ID with full nested structure.

    Returns course metadata, legacy source materials, module
    hierarchy, and the material tree with attached entries
    and their lifecycle states.
    """
    repo = CourseRepository(session, tenant.tenant_id)
    course = await repo.get_with_structure(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    node_repo = MaterialNodeRepository(session)
    tree_roots = await node_repo.get_tree(course_id, include_materials=True)
    tree = [NodeWithMaterialsResponse.model_validate(r) for r in tree_roots]

    # Compute course-level fingerprint from root node fingerprints.
    # Returns None if any root has no node_fingerprint (needs recompute).
    course_fp: str | None = None
    if tree_roots:
        from course_supporter.fingerprint import FingerprintService

        fp_service = FingerprintService(session)
        try:
            course_fp = await fp_service.ensure_course_fp(tree_roots)
        except ValueError:
            course_fp = None

    response = CourseDetailResponse.model_validate(course)
    response.material_tree = tree
    response.course_fingerprint = course_fp
    return response


@router.post("/courses/{course_id}/nodes/{node_id}/slide-mapping", status_code=201)
async def create_slide_mapping(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    body: SlideVideoMapRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> SlideVideoMapResponse:
    """Create slide-video mappings for a material node."""
    course_repo = CourseRepository(session, tenant.tenant_id)
    course = await course_repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    node_repo = MaterialNodeRepository(session)
    node = await node_repo.get_by_id(node_id)
    if node is None or node.course_id != course.id:
        raise HTTPException(status_code=404, detail="Node not found")

    # ── Validation (L1 structural + L2 content + L3 deferred) ──
    validator = MappingValidationService(session)
    results = await validator.validate_batch(node_id, body.mappings)

    failed = [
        r for r in results if r.status == MappingValidationState.VALIDATION_FAILED
    ]
    if failed:
        raise HTTPException(
            status_code=422,
            detail=[
                {"index": r.index, "errors": [_ve_to_dict(e) for e in r.errors]}
                for r in failed
            ],
        )

    svm_repo = SlideVideoMappingRepository(session)
    records = await svm_repo.batch_create(
        node_id, body.mappings, validation_results=results
    )
    await session.commit()
    return SlideVideoMapResponse(
        created=len(records),
        mappings=[SlideVideoMapItemResponse.model_validate(r) for r in records],
    )


@router.get("/courses/{course_id}/lessons/{lesson_id}")
async def get_lesson(
    course_id: uuid.UUID,
    lesson_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> LessonDetailResponse:
    """Get lesson by ID within a course."""
    repo = LessonRepository(session)
    lesson = await repo.get_by_id_for_course(lesson_id, course_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return LessonDetailResponse.model_validate(lesson)


@router.post("/courses/{course_id}/materials", status_code=201)
async def create_material(
    course_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    source_type: Annotated[
        SourceType,
        Form(description="Material type: video, presentation, text, or web."),
    ],
    source_url: Annotated[
        str | None,
        Form(description="URL to the source material. Required if no file."),
    ] = None,
    file: Annotated[
        UploadFile | None,
        Form(
            description=(
                "File upload (multipart). Accepted formats: "
                "video (mp4, webm, mkv, avi), "
                "presentation (pdf, pptx), "
                "text (md, txt, docx, html). "
                "Required if source_url is not provided."
            ),
        ),
    ] = None,
) -> MaterialCreateResponse:
    """Create a source material for a course.

    Accepts either a URL or a file upload. If a file is provided,
    it is uploaded to S3/MinIO and the resulting URL is stored.
    Ingestion is enqueued via ARQ for background processing.

    File type validation per source_type:

    - **video**: .mp4, .webm, .mkv, .avi
    - **presentation**: .pdf, .pptx
    - **text**: .md, .markdown, .docx, .html, .htm, .txt
    - **web**: URL only, file upload not allowed
    """

    if source_url is None and file is None:
        raise HTTPException(
            status_code=422,
            detail="Either source_url or file must be provided",
        )

    if file is not None:
        if source_type == SourceType.WEB:
            raise HTTPException(
                status_code=422,
                detail="source_type 'web' does not accept file uploads,"
                " provide source_url instead.",
            )
        allowed = ALLOWED_EXTENSIONS.get(source_type, frozenset())
        ext = _file_extension(file.filename)
        if ext not in allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"File extension '{ext}' is not allowed "
                    f"for source_type '{source_type}'. "
                    f"Accepted: {sorted(allowed)}"
                ),
            )

    course_repo = CourseRepository(session, tenant.tenant_id)
    course = await course_repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    filename: str | None = None
    actual_url: str

    if file is not None:
        filename = file.filename
        key = f"{course_id}/{uuid.uuid4()}/{filename or 'upload'}"
        content_type = file.content_type or "application/octet-stream"
        actual_url, uploaded_bytes = await s3.upload_smart(
            stream=upload_file_chunks(file),
            key=key,
            content_type=content_type,
            file_size=file.size,
        )
        logger.info("file_uploaded", key=key, size=uploaded_bytes)
    elif source_url is not None:
        actual_url = source_url

    repo = SourceMaterialRepository(session)
    material = await repo.create(
        course_id=course_id,
        source_type=source_type,
        source_url=actual_url,
        filename=filename,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        course_id=course_id,
        material_id=material.id,
        source_type=source_type,
        source_url=actual_url,
    )
    await session.commit()

    return MaterialCreateResponse(
        id=material.id,
        source_type=material.source_type,
        source_url=material.source_url,
        filename=material.filename,
        status=material.status,
        created_at=material.created_at,
        job_id=job.id,
    )
