"""Course management API endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_s3_client, get_session
from course_supporter.api.schemas import (
    CourseCreateRequest,
    CourseDetailResponse,
    CourseResponse,
    LessonDetailResponse,
    MaterialCreateResponse,
    SlideVideoMapItemResponse,
    SlideVideoMapRequest,
    SlideVideoMapResponse,
)
from course_supporter.api.tasks import ingest_material
from course_supporter.models.source import SourceType
from course_supporter.storage.database import async_session
from course_supporter.storage.repositories import (
    CourseRepository,
    LessonRepository,
    SlideVideoMappingRepository,
    SourceMaterialRepository,
)
from course_supporter.storage.s3 import S3Client

router = APIRouter(tags=["courses"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]

VALID_SOURCE_TYPES = {t.value for t in SourceType}


@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    session: SessionDep,
) -> CourseResponse:
    """Create a new course."""
    repo = CourseRepository(session)
    course = await repo.create(title=body.title, description=body.description)
    await session.commit()
    return CourseResponse.model_validate(course)


@router.get("/courses/{course_id}")
async def get_course(
    course_id: uuid.UUID,
    session: SessionDep,
) -> CourseDetailResponse:
    """Get course by ID with full nested structure."""
    repo = CourseRepository(session)
    course = await repo.get_with_structure(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return CourseDetailResponse.model_validate(course)


@router.post("/courses/{course_id}/slide-mapping", status_code=201)
async def create_slide_mapping(
    course_id: uuid.UUID,
    body: SlideVideoMapRequest,
    session: SessionDep,
) -> SlideVideoMapResponse:
    """Create slide-video mappings for a course."""
    course_repo = CourseRepository(session)
    course = await course_repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    svm_repo = SlideVideoMappingRepository(session)
    records = await svm_repo.batch_create(course_id, body.mappings)
    await session.commit()
    return SlideVideoMapResponse(
        created=len(records),
        mappings=[SlideVideoMapItemResponse.model_validate(r) for r in records],
    )


@router.get("/courses/{course_id}/lessons/{lesson_id}")
async def get_lesson(
    course_id: uuid.UUID,
    lesson_id: uuid.UUID,
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
    background_tasks: BackgroundTasks,
    session: SessionDep,
    s3: S3Dep,
    source_type: Annotated[str, Form()],
    source_url: Annotated[str | None, Form()] = None,
    file: UploadFile | None = None,
) -> MaterialCreateResponse:
    """Create a source material for a course.

    Accepts either a URL or a file upload. If a file is provided,
    it is uploaded to S3/MinIO and the resulting URL is stored.
    Background ingestion is triggered automatically.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid source_type. Must be one of: {sorted(VALID_SOURCE_TYPES)}",
        )

    if source_url is None and file is None:
        raise HTTPException(
            status_code=422,
            detail="Either source_url or file must be provided",
        )

    course_repo = CourseRepository(session)
    course = await course_repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")

    filename: str | None = None
    actual_url: str

    if file is not None:
        filename = file.filename
        content = await file.read()
        key = f"{course_id}/{uuid.uuid4()}/{filename or 'upload'}"
        actual_url = await s3.upload_file(
            key, content, file.content_type or "application/octet-stream"
        )
    else:
        assert source_url is not None  # guaranteed by earlier validation
        actual_url = source_url

    repo = SourceMaterialRepository(session)
    material = await repo.create(
        course_id=course_id,
        source_type=source_type,
        source_url=actual_url,
        filename=filename,
    )
    await session.commit()

    background_tasks.add_task(
        ingest_material,
        material.id,
        source_type,
        actual_url,
        async_session,
    )

    return MaterialCreateResponse.model_validate(material)
