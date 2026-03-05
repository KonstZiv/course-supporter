"""Material entry management API endpoints.

Provides CRUD operations for materials attached to tree nodes.
Each material goes through a lifecycle (RAW → PENDING → READY/ERROR)
tracked via the derived ``state`` property. Ingestion is auto-enqueued
on creation and can be retried on failure.

Tenant isolation is enforced by verifying course ownership
before accessing any node or material.

Routes
------
- ``POST   /courses/{id}/nodes/{nid}/materials``       — Add material to node
- ``GET    /courses/{id}/nodes/{nid}/materials``        — List materials for node
- ``GET    /courses/{id}/materials/{mid}``              — Get single material
- ``DELETE /courses/{id}/materials/{mid}``              — Delete material
- ``POST   /courses/{id}/materials/{mid}/retry``        — Retry failed ingestion
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    MaterialEntryCreateResponse,
    MaterialEntryResponse,
)
from course_supporter.api.upload_validation import ALLOWED_EXTENSIONS, file_extension
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_ingestion
from course_supporter.models.source import SourceType
from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MaterialEntry
from course_supporter.storage.repositories import CourseRepository
from course_supporter.storage.s3 import S3Client, upload_file_chunks

logger = structlog.get_logger()

router = APIRouter(tags=["materials"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


async def _require_course(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    course_id: uuid.UUID,
) -> None:
    """Verify the course exists and belongs to the tenant.

    Raises:
        HTTPException 404: If the course is not found or
            does not belong to the authenticated tenant.
    """
    repo = CourseRepository(session, tenant_id)
    course = await repo.get_by_id(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")


async def _require_node(
    session: AsyncSession,
    node_id: uuid.UUID,
    course_id: uuid.UUID,
) -> None:
    """Verify the node exists and belongs to the course.

    Raises:
        HTTPException 404: If the node is not found or
            belongs to a different course.
    """
    repo = MaterialNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.course_id != course_id:
        raise HTTPException(status_code=404, detail="Node not found")


async def _require_material(
    entry_repo: MaterialEntryRepository,
    node_repo: MaterialNodeRepository,
    entry_id: uuid.UUID,
    course_id: uuid.UUID,
) -> MaterialEntry:
    """Verify the material exists and belongs to the course.

    Checks MaterialEntry → MaterialNode → Course chain.

    Raises:
        HTTPException 404: If the material is not found or
            belongs to a different course.
    """
    entry = await entry_repo.get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Material not found")

    node = await node_repo.get_by_id(entry.node_id)
    if node is None or node.course_id != course_id:
        raise HTTPException(status_code=404, detail="Material not found")
    return entry


@router.post("/courses/{course_id}/nodes/{node_id}/materials", status_code=201)
async def create_material(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
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
        File(
            description=(
                "File upload (multipart). Accepted formats: "
                "presentation (pdf, pptx), text (md, txt, docx, html), "
                "video (mp4, webm, mkv, avi). "
                "Required if source_url is not provided."
            ),
        ),
    ] = None,
    filename: Annotated[
        str | None,
        Form(description="Override filename (optional, defaults to uploaded name)."),
    ] = None,
) -> MaterialEntryCreateResponse:
    """Add a new material to a tree node.

    Accepts either a URL or a file upload. If a file is provided,
    it is uploaded to S3/MinIO and the resulting URL is stored.

    Creates a ``MaterialEntry`` and auto-enqueues an ingestion job
    via ARQ. The ``job_id`` in the response can be used to track
    processing status via ``GET /api/v1/jobs/{job_id}``.

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
        ext = file_extension(file.filename)
        if ext not in allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"File extension '{ext}' is not allowed "
                    f"for source_type '{source_type}'. "
                    f"Accepted: {sorted(allowed)}"
                ),
            )

    await _require_course(session, tenant.tenant_id, course_id)
    await _require_node(session, node_id, course_id)

    actual_filename: str | None = filename
    actual_url: str

    if file is not None:
        if actual_filename is None:
            actual_filename = file.filename
        key = f"{course_id}/{node_id}/{uuid.uuid4()}/{actual_filename or 'upload'}"
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

    entry_repo = MaterialEntryRepository(session)
    entry = await entry_repo.create(
        node_id=node_id,
        source_type=source_type,
        source_url=actual_url,
        filename=actual_filename,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        course_id=course_id,
        material_id=entry.id,
        source_type=source_type,
        source_url=actual_url,
    )
    await session.commit()

    logger.info(
        "material_entry_created",
        entry_id=str(entry.id),
        node_id=str(node_id),
        course_id=str(course_id),
        job_id=str(job.id),
    )
    response = MaterialEntryCreateResponse.model_validate(entry)
    response.job_id = job.id
    return response


@router.get("/courses/{course_id}/nodes/{node_id}/materials")
async def list_materials(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[MaterialEntryResponse]:
    """List all materials attached to a tree node.

    Returns materials ordered by their position (``order`` field).
    Each material includes the derived ``state`` indicating its
    lifecycle stage.

    Returns an empty list if the node has no materials.
    """
    await _require_course(session, tenant.tenant_id, course_id)
    await _require_node(session, node_id, course_id)

    repo = MaterialEntryRepository(session)
    entries = await repo.get_for_node(node_id)
    return [MaterialEntryResponse.model_validate(e) for e in entries]


@router.get("/courses/{course_id}/materials/{entry_id}")
async def get_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> MaterialEntryResponse:
    """Get a single material entry by ID.

    The material must belong to the specified course
    (verified through the node → course chain).
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)
    return MaterialEntryResponse.model_validate(entry)


@router.delete("/courses/{course_id}/materials/{entry_id}", status_code=204)
async def delete_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> None:
    """Delete a material entry.

    Removes the material and its processed content permanently.
    If an ingestion job is in progress, it will fail gracefully
    when it tries to write back results.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)

    await entry_repo.delete(entry.id)
    await session.commit()

    logger.info(
        "material_entry_deleted",
        entry_id=str(entry_id),
        course_id=str(course_id),
    )


@router.post("/courses/{course_id}/materials/{entry_id}/retry")
async def retry_material(
    course_id: uuid.UUID,
    entry_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> MaterialEntryCreateResponse:
    """Retry ingestion for a failed material.

    Only materials in ``error`` state can be retried. This clears
    the error, creates a new ingestion job, and returns the updated
    material with the new ``job_id``.

    Returns 409 if the material is not in ``error`` state.
    """
    await _require_course(session, tenant.tenant_id, course_id)

    entry_repo = MaterialEntryRepository(session)
    node_repo = MaterialNodeRepository(session)
    entry = await _require_material(entry_repo, node_repo, entry_id, course_id)

    if entry.state != "error":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry: material is in '{entry.state}' state, expected 'error'."
            ),
        )

    # Clear error and re-enqueue
    entry.error_message = None
    await session.flush()

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        course_id=course_id,
        material_id=entry.id,
        source_type=entry.source_type,
        source_url=entry.source_url,
    )
    await session.commit()

    logger.info(
        "material_entry_retry",
        entry_id=str(entry_id),
        course_id=str(course_id),
        job_id=str(job.id),
    )
    response = MaterialEntryCreateResponse.model_validate(entry)
    response.job_id = job.id
    return response
