"""Authored document management API endpoints.

Provides CRUD operations for authored documents attached to course
tree nodes. Each document goes through a lifecycle (RAW → PENDING →
READY/ERROR) tracked via the derived ``state`` property. Ingestion
is auto-enqueued on creation and can be retried on failure.

Tenant isolation is enforced by verifying node ownership via tenant_id.

Routes
------
- ``POST   /nodes/{nid}/documents``                — Add document to node
- ``POST   /nodes/{nid}/documents/upload-url``     — Get presigned upload URL
- ``POST   /nodes/{nid}/documents/confirm-upload`` — Confirm presigned upload
- ``GET    /nodes/{nid}/documents``                — List documents for node
- ``GET    /documents/{did}``                      — Get single document
- ``PATCH  /documents/{did}``                      — Update document
- ``DELETE /documents/{did}``                      — Delete document (KD3 cascade)
- ``POST   /documents/{did}/retry``                — Retry failed ingestion
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Final

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    AuthoredDocumentCreateResponse,
    AuthoredDocumentResponse,
    AuthoredDocumentUpdateRequest,
    ConfirmUploadRequest,
    PresignedUrlRequest,
    PresignedUrlResponse,
    ProcessingEstimate,
    ProjectBaseAttachResponse,
    ProjectBaseManifestResponse,
    ProjectBaseStateResponse,
)
from course_supporter.api.upload_validation import check_platform
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.config import get_settings
from course_supporter.enqueue import enqueue_base_normalize, enqueue_ingestion
from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video_pipeline import media
from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.language import (
    InvalidLanguageError,
    LanguageNotAllowedError,
    normalize_and_validate,
)
from course_supporter.models.source import AssignmentType, MaterialRole, SourceType
from course_supporter.normalizer import manifest_from_jsonb
from course_supporter.security import AUTHORED_POLICY, run_stage1
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.file_type import extension_of
from course_supporter.security.policies import (
    CODE_EXTENSIONS,
    get_max_size_for_extension,
)
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.cascade import CascadeDeleteService, build_cascade_map
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client, sanitize_s3_key, upload_file_chunks

logger = structlog.get_logger()

router = APIRouter(tags=["documents"])

# KD18 P2 (Decision 1, 1A): raw-upload size cap for an author base archive
# (compressed bytes). Distinct from the shared _PROJECT_NORMALIZE_LIMITS, which
# bound the UNPACK. Enforced by streaming cutoff (never buffer-then-check).
_BASE_ARCHIVE_MAX_UPLOAD_BYTES: Final[int] = 100 * 1024 * 1024
_BASE_UPLOAD_CHUNK_BYTES: Final[int] = 1024 * 1024


async def _read_upload_capped(file: UploadFile, cap: int) -> bytes:
    """Read an upload fully into memory, enforcing ``cap`` DURING the read.

    A fast pre-check on the declared ``Content-Length`` rejects an oversize
    upload before reading a byte; the chunked loop then re-enforces the cap
    against the ACTUAL bytes (Content-Length can lie), stopping the moment the
    running total exceeds ``cap`` — the upload is never fully buffered when
    oversize, and no S3 write happens on rejection. Raises ``413``.
    """
    if file.size is not None and file.size > cap:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "UPLOAD_TOO_LARGE",
                "details": f"upload {file.size} bytes exceeds base archive cap {cap}",
            },
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_BASE_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "UPLOAD_TOO_LARGE",
                    "details": f"upload exceeds base archive cap {cap} bytes",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _enforce_code_routing(source_type: SourceType, upload_ext: str) -> None:
    """Enforce the code↔extension routing invariant on both upload paths.

    Two directions (task-code-materials commit 3):

    * ``source_type=code`` accepts only a source-file extension
      (:data:`CODE_EXTENSIONS`) or a ``.zip`` project archive (R1).
    * ``.zip`` exists in the authored whitelist ONLY for code — a zip
      declared as any other source_type has no processor and would die
      async; fail it fast and deterministically here instead.

    Raises:
        HTTPException: 400 SECURITY_REJECTED / FORBIDDEN_TYPE when a
            code upload carries a non-code extension; 422 structured
            when an archive is declared as a non-code source_type.
    """
    if source_type == SourceType.CODE:
        if upload_ext != "zip" and upload_ext not in CODE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "SECURITY_REJECTED",
                    "category": ErrorCategory.FORBIDDEN_TYPE.value,
                    "details": (
                        f"extension {upload_ext!r} is not a recognised source-code "
                        f"extension or .zip archive for source_type 'code'"
                    ),
                },
            )
    elif upload_ext == "zip":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARCHIVE_REQUIRES_CODE",
                "details": (
                    "archive uploads are accepted only as source_type='code' "
                    "project archives"
                ),
            },
        )


def _processing_estimate() -> ProcessingEstimate:
    """Build the DD-3.3c-I-B queue-wait hint from operator-tuned settings."""
    s = get_settings()
    return ProcessingEstimate(
        max_hours=s.intake_hint_max_hours,
        check_after_hours=s.intake_hint_check_after_hours,
    )


async def _intake_video_duration_sec(
    source_type: SourceType,
    *,
    content: bytes | None = None,
    url: str | None = None,
    filename: str = "upload",
) -> float | None:
    """Probe a video's duration at intake (DD-3.3c-I-B), else ``None``.

    Returns ``None`` for non-video source types (they carry no video-hours
    and so never gate). For video, ffprobes the already-local upload
    ``content`` or resolves the remote ``url`` via yt-dlp metadata.

    Probe failure maps to a clear status, never a bare 500:

    * :class:`UnsupportedFormatError` — the input itself is bad
      (private/unavailable/corrupt/non-video/no derivable duration). 422
      ``INTAKE_DURATION_UNAVAILABLE``; retrying the same input won't help.
    * :class:`ProcessingError` — the probe operation failed transiently
      (yt-dlp / ffprobe timeout, or missing binary; both raised by
      ``media._run``). 503 ``INTAKE_PROBE_UNAVAILABLE`` + ``Retry-After``;
      retrying makes sense. Mirrors the gate's 503. Ordered AFTER the
      ``UnsupportedFormatError`` clause — it is a subclass, so the specific
      input-error case must match first.
    """
    if source_type != SourceType.VIDEO:
        return None
    try:
        if content is not None:
            return await media.probe_intake_duration_from_bytes(
                content, filename=filename
            )
        if url is None:
            # Programmer error — every callsite supplies content or url. An
            # explicit raise (not assert, which -O strips) turns a missing
            # invariant into a loud failure instead of a silent None probe.
            msg = "video intake probe requires either content or url"
            raise ValueError(msg)
        return await media.probe_intake_duration_sec(url)
    except UnsupportedFormatError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INTAKE_DURATION_UNAVAILABLE",
                "category": "duration_probe",
                "details": str(exc),
            },
        ) from exc
    except ProcessingError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INTAKE_PROBE_UNAVAILABLE",
                "category": "duration_probe",
                "details": (
                    f"Could not reach the video source to determine its "
                    f"duration ({exc}). This is usually transient — please "
                    f"try again."
                ),
            },
            headers={
                "Retry-After": str(
                    int(get_settings().intake_hint_check_after_hours * 3600)
                )
            },
        ) from exc


SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

# KD-2.3-I / KD-2.3-M — HTTP-side slide-count cap for presentation uploads.
# Fast pre-validation (<200 ms) keeps the heavy LibreOffice convert in the
# ARQ worker (sub-area #4 / #7), not the request handler.
_MAX_PRESENTATION_SLIDES: Final[int] = 100


def _presentation_slide_count(ext: str, content: bytes) -> int | None:
    """Count slides/pages of a presentation upload for the slide-count gate.

    Returns the count for the formats inspectable in-process well under the
    KD-2.3-I 200 ms budget:

    * ``pdf`` -- PyMuPDF page count (mirrors the worker
      ``_extract_pdf_pages`` open pattern).
    * ``pptx`` -- python-pptx slide count.

    Returns ``None`` (skip the gate, defer to the worker) for:

    * ``ppt`` -- legacy binary PowerPoint; python-pptx reads only OOXML
      ``.pptx`` and PyMuPDF cannot open ``.ppt``.
    * a magic-valid but structurally corrupt ``pdf`` / ``pptx`` payload --
      it cannot be counted HTTP-side, so the gate is skipped and the worker
      enforces authoritatively after the LibreOffice convert (CA-3) -- the
      same defer-to-worker semantic as the ``.ppt`` skip.
    """
    ext_lower = ext.lower()
    if ext_lower not in ("pdf", "pptx"):
        # ``ppt`` (legacy binary) and any non-presentation extension: skip.
        return None

    import io
    import zipfile

    import fitz
    from pptx import Presentation
    from pptx.exc import PackageNotFoundError

    try:
        if ext_lower == "pdf":
            with fitz.open(stream=content, filetype="pdf") as doc:
                return int(doc.page_count)
        return len(Presentation(io.BytesIO(content)).slides)
    except (fitz.FileDataError, PackageNotFoundError, zipfile.BadZipFile) as exc:
        # Magic-valid but structurally corrupt payload: skip the gate and let
        # the worker enforce post-LibreOffice (CA-3). Narrow catch only --
        # broad ``except`` would mask real bugs (Rule #14 spirit). ``KeyError``
        # (a valid ZIP that is not a PPTX package) is deliberately NOT caught:
        # run_stage1's libmagic MIME check rejects a non-PPTX ZIP as a type
        # mismatch before this runs in production, and KeyError is too generic
        # to swallow safely.
        logger.warning(
            "slide_count_inspect_failed",
            extension=ext_lower,
            byte_size=len(content),
            exception_type=type(exc).__name__,
        )
        return None


def _enforce_presentation_slide_cap(
    source_type: SourceType, ext: str, content: bytes
) -> None:
    """Raise on a presentation upload that exceeds the slide cap.

    No-op unless ``source_type`` is ``PRESENTATION`` and the format is
    HTTP-inspectable and over :data:`_MAX_PRESENTATION_SLIDES`. Reuses
    :class:`SecurityRejectedError` (KD-2.3-M — no new exception class); the
    caller's existing ``except SecurityRejectedError`` block maps it to the
    uniform HTTP 400 ``SECURITY_REJECTED`` shape.
    """
    if source_type != SourceType.PRESENTATION:
        return
    count = _presentation_slide_count(ext, content)
    if count is not None and count > _MAX_PRESENTATION_SLIDES:
        raise SecurityRejectedError(
            ErrorCategory.SLIDE_COUNT_LIMIT,
            f"slide count {count} exceeds presentation limit "
            f"{_MAX_PRESENTATION_SLIDES}",
        )


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the tenant."""
    repo = CourseNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


async def _require_document_for_tenant(
    document_repo: AuthoredDocumentRepository,
    node_repo: CourseNodeRepository,
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> AuthoredDocument:
    """Verify the document exists and belongs to the tenant.

    Checks AuthoredDocument → CourseNode → tenant_id chain.
    """
    document = await document_repo.get_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    node = await node_repo.get_by_id(document.course_node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.post("/nodes/{node_id}/documents", status_code=201)
async def create_document(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    source_type: Annotated[
        SourceType,
        Form(
            description="Document type: video, presentation, text, web, "
            "audio, or code.",
        ),
    ],
    material_role: Annotated[
        MaterialRole,
        Form(
            description="Role: educational (delivers content) "
            "or methodological (declares course intent).",
        ),
    ] = MaterialRole.EDUCATIONAL,
    task_type: Annotated[
        AssignmentType | None,
        Form(
            description=(
                "Mark the document as a concrete task of the given taxonomy "
                "tier (test, short_task, task, project). Omit for regular "
                "documents."
            ),
        ),
    ] = None,
    source_url: Annotated[
        str | None,
        Form(description="URL to the source document. Required if no file."),
    ] = None,
    file: Annotated[
        UploadFile | None,
        File(
            description=(
                "File upload (multipart). Accepted formats: "
                "presentation (pdf, pptx), text (md, txt, docx, html), "
                "video (mp4, webm, mkv, avi), audio (mp3, wav, m4a, ogg, "
                "flac), code (source files or a .zip project archive). "
                "Required if source_url is not provided."
            ),
        ),
    ] = None,
    filename: Annotated[
        str | None,
        Form(description="Override filename (optional, defaults to uploaded name)."),
    ] = None,
    language: Annotated[
        str | None,
        Form(
            description=(
                "Optional language override. Accepts ISO 639-1 / 639-3 / "
                "English name; stored as canonical ISO 639-3. When empty, "
                "the course default is used and STT falls back to "
                "auto-detection."
            ),
        ),
    ] = None,
) -> AuthoredDocumentCreateResponse:
    """Add a new authored document to a tree node.

    Accepts either a URL or a file upload. If a file is provided,
    it is uploaded to S3/MinIO and the resulting URL is stored.

    Creates an ``AuthoredDocument`` and auto-enqueues an ingestion job
    via ARQ. The ``job_id`` in the response can be used to track
    processing status via ``GET /api/v1/jobs/{job_id}``.
    """
    if source_url is None and file is None:
        raise HTTPException(
            status_code=422,
            detail="Either source_url or file must be provided",
        )

    # FastAPI ``Form()`` does not run Pydantic field validators, so the
    # language helper is invoked explicitly here. Without the explicit
    # ``HTTPException``, the helper's ``ValueError`` subclasses would
    # bubble as a 500 instead of a 422 (Task 2.4.13 fix-1 ratify).
    if language is not None:
        try:
            language = normalize_and_validate(language)
        except (InvalidLanguageError, LanguageNotAllowedError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if source_type == SourceType.CODE and file is None:
        raise HTTPException(
            status_code=422,
            detail="source_type 'code' requires a file upload "
            "(single source file or a .zip project archive).",
        )

    if file is not None:
        if source_type == SourceType.WEB:
            raise HTTPException(
                status_code=422,
                detail="source_type 'web' does not accept file uploads,"
                " provide source_url instead.",
            )
        # KD14 Stage 1 — full validation (extension whitelist + libmagic
        # MIME + size cap + charset/regex/unicode for text + archive
        # structure for archives) per Phase 1.2 §2.3 ratify. AUTHORED_POLICY
        # drives the whitelist; legacy source_type-segmented hand-rolled
        # ALLOWED_EXTENSIONS was removed (8-extension behavioral drift —
        # see POST-MERGE-NOTES "Ratified whitelist drift").
        upload_filename = file.filename or "upload"
        upload_ext = extension_of(upload_filename)
        _enforce_code_routing(source_type, upload_ext)
        # Pre-read size check — fast-fails clearly oversize files BEFORE
        # ``await file.read()`` loads bytes into memory (per pre-flight §6.2
        # large-file memory observation; 5 GB video cap is operational
        # concern, не security).
        max_size = get_max_size_for_extension(upload_ext, AUTHORED_POLICY)
        if file.size is not None and file.size > max_size:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "SECURITY_REJECTED",
                    "category": ErrorCategory.SIZE_LIMIT.value,
                    "details": (
                        f"file size {file.size} bytes exceeds authored "
                        f"limit {max_size} bytes for {upload_ext!r}"
                    ),
                },
            )
        upload_content = await file.read()
        await file.seek(0)
        if source_type == SourceType.CODE:
            # Light gate (task-code-materials R8, base-attach precedent
            # documents.py attach_project_base): extension routing + size
            # cap only. The fail-closed ``run_stage1`` is deliberately NOT
            # on this path — its strict archive branch would reject a real
            # project's non-allowlisted members (lockfiles, dotfiles); the
            # structural sandbox is ``extract_archive_safely(classify=...)``
            # inside the CodeProcessor worker, and content safety is the
            # async Stage-2 check over the raw assembled text (F6).
            pass
        else:
            try:
                run_stage1(
                    filename=upload_filename,
                    content=upload_content,
                    context="authored",
                )
                _enforce_presentation_slide_cap(source_type, upload_ext, upload_content)
            except SecurityRejectedError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "SECURITY_REJECTED",
                        "category": exc.category.value,
                        "details": exc.detail,
                    },
                ) from exc

    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    actual_filename: str | None = filename
    actual_url: str

    raw_hash: str | None = None
    raw_size_bytes: int | None = None
    duration_sec: float | None = None
    if file is not None:
        # KD-2.1-E — Strategy A: SHA-256 from the bytes already loaded
        # for Stage 1 (line ~217 above). No streaming wrapper needed --
        # Stage 1 forces full-bytes-in-memory anyway, hash compute is
        # zero marginal cost on those same bytes. Closes DD-1.1-1
        # (AuthoredDocument.raw_hash never populated production-wide).
        # Pair-populate raw_size_bytes per INVESTIGATION.md §6.5 Strategy A
        # original design (sealed PHASE.md §"Коміт 8" silent on the size
        # half of the pair — D17 acknowledged deviation).
        raw_hash = hashlib.sha256(upload_content).hexdigest()
        raw_size_bytes = len(upload_content)
        if actual_filename is None:
            actual_filename = file.filename
        safe_name = sanitize_s3_key(actual_filename or "upload")
        key = f"{node_id}/{uuid.uuid4()}/{safe_name}"
        content_type = file.content_type or "application/octet-stream"
        actual_url, uploaded_bytes = await s3.upload_smart(
            stream=upload_file_chunks(file),
            key=key,
            content_type=content_type,
            file_size=file.size,
        )
        logger.info("file_uploaded", key=key, size=uploaded_bytes)
        # DD-3.3c-I-B: probe video duration at intake (ffprobe on the
        # already-in-memory upload bytes) for the admission gate.
        duration_sec = await _intake_video_duration_sec(
            source_type, content=upload_content, filename=upload_filename
        )
    elif source_url is not None:
        actual_url = source_url
        # DD-3.3c-I-B: resolve video duration via yt-dlp metadata (no
        # download) for the admission gate.
        duration_sec = await _intake_video_duration_sec(source_type, url=actual_url)

    document_repo = AuthoredDocumentRepository(session)
    document = await document_repo.create(
        node_id=node_id,
        source_type=source_type,
        source_url=actual_url,
        filename=actual_filename,
        material_role=material_role,
        task_type=task_type,
        language=language,
        raw_hash=raw_hash,
        raw_size_bytes=raw_size_bytes,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
        material_id=document.id,
        source_type=source_type,
        source_url=actual_url,
        duration_sec=duration_sec,
    )
    # enqueue_ingestion owns the commit (QQ5 helper-owns-commit) — it has
    # already durably committed the document INSERT + Job before dispatch.

    logger.info(
        "document_created",
        document_id=str(document.id),
        node_id=str(node_id),
        job_id=str(job.id),
        task_type=task_type,
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id
    response.processing_estimate = _processing_estimate()

    warning = check_platform(source_type, actual_url)
    if warning:
        response.warnings.append(warning)

    return response


PRESIGNED_URL_EXPIRY = 900  # 15 minutes


@router.post("/nodes/{node_id}/documents/upload-url")
async def get_upload_url(
    node_id: uuid.UUID,
    body: PresignedUrlRequest,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
) -> PresignedUrlResponse:
    """Generate a presigned URL for direct S3 upload.

    The client should PUT the file content to the returned URL,
    then call ``POST /nodes/{nid}/documents/confirm-upload`` with
    the returned key.
    """
    if body.source_type == SourceType.WEB:
        raise HTTPException(
            status_code=422,
            detail="source_type 'web' does not support file upload.",
        )

    # KD14 ext-only direct check pre-presigned-upload per Phase 1.2 §6.1 (a)
    # ratify: file content unavailable until S3 PUT completes, so full
    # Stage 1 (libmagic / size / charset / archive structure) cannot run
    # here. The full Stage 1 is invoked in ``create_document`` (multipart
    # path); ``confirm_upload`` Stage 1 wiring deferred to Phase 2.1 (see
    # POST-MERGE-NOTES "Known limitations"). Ext-allowlist fast-fails
    # clear-cut bad extensions before client uploads MB+ of rejected
    # content — UX parity with legacy hand-rolled ext check.
    upload_ext = extension_of(body.filename)
    if upload_ext not in AUTHORED_POLICY.allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SECURITY_REJECTED",
                "category": ErrorCategory.FORBIDDEN_TYPE.value,
                "details": (
                    f"extension {upload_ext!r} not allowed for authored uploads"
                ),
            },
        )
    # Code routing invariant — fast-fail BEFORE the client PUTs MB+ of
    # content that confirm_upload would reject anyway (UX parity with the
    # ext-allowlist check above).
    _enforce_code_routing(body.source_type, upload_ext)

    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    safe_name = sanitize_s3_key(body.filename)
    key = f"tenants/{tenant.tenant_id}/nodes/{node_id}/{uuid.uuid4()}/{safe_name}"

    upload_url = await s3.generate_presigned_url(
        key, body.content_type, expires_in=PRESIGNED_URL_EXPIRY
    )

    logger.info("presigned_url_generated", key=key, node_id=str(node_id))
    return PresignedUrlResponse(
        upload_url=upload_url,
        key=key,
        expires_in=PRESIGNED_URL_EXPIRY,
    )


@router.post("/nodes/{node_id}/documents/confirm-upload", status_code=201)
async def confirm_upload(
    node_id: uuid.UUID,
    body: ConfirmUploadRequest,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
) -> AuthoredDocumentCreateResponse:
    """Confirm a presigned upload and create the AuthoredDocument.

    Verifies the file exists in S3, creates the database entry,
    and enqueues ingestion.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # Verify key belongs to this tenant and node
    expected_prefix = f"tenants/{tenant.tenant_id}/nodes/{node_id}/"
    if not body.key.startswith(expected_prefix):
        raise HTTPException(
            status_code=403,
            detail="S3 key does not match tenant/node.",
        )

    # Verify file exists in S3
    try:
        head_response = await s3.head_object(body.key)
    except Exception:
        raise HTTPException(  # noqa: B904
            status_code=404,
            detail=("File not found in S3. Upload may have failed or expired."),
        )

    actual_filename = body.filename or body.key.rsplit("/", 1)[-1]

    # KD-2.1-D — full Stage 1 on the confirm_upload path. Mirrors the
    # multipart create_document Stage 1 block (lines ~192-233). Size
    # pre-check via head_response.ContentLength fast-fails before the
    # bytes fetch; full Stage 1 (libmagic MIME + extension match +
    # archive structure + unicode + regex) gates AuthoredDocument
    # creation. Closes DD-1.2-4 — confirm_upload was Stage-1-less
    # since Phase 1.2 (KD14 explicit gap noted in §6.1(a) ratify).
    upload_ext = extension_of(actual_filename)
    _enforce_code_routing(body.source_type, upload_ext)
    max_size = get_max_size_for_extension(upload_ext, AUTHORED_POLICY)
    content_length = head_response["ContentLength"]
    if content_length > max_size:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SECURITY_REJECTED",
                "category": ErrorCategory.SIZE_LIMIT.value,
                "details": (
                    f"file size {content_length} bytes exceeds authored "
                    f"limit {max_size} bytes for {upload_ext!r}"
                ),
            },
        )

    body_bytes = await s3.get_object(body.key)
    if body.source_type == SourceType.CODE:
        # Light gate (task-code-materials R8): mirror of the multipart
        # create_document code branch — extension routing + size cap only;
        # ``run_stage1`` deliberately skipped (see create_document).
        pass
    else:
        try:
            run_stage1(
                filename=actual_filename,
                content=body_bytes,
                context="authored",
            )
            _enforce_presentation_slide_cap(body.source_type, upload_ext, body_bytes)
        except SecurityRejectedError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "SECURITY_REJECTED",
                    "category": exc.category.value,
                    "details": exc.detail,
                },
            ) from exc

    s3_url = s3.get_object_url(body.key)

    # DD-3.3c-I-B: probe video duration at intake (ffprobe on the fetched
    # S3 bytes) for the admission gate.
    duration_sec = await _intake_video_duration_sec(
        body.source_type, content=body_bytes, filename=actual_filename
    )

    document_repo = AuthoredDocumentRepository(session)
    document = await document_repo.create(
        node_id=node_id,
        source_type=body.source_type,
        source_url=s3_url,
        filename=actual_filename,
        material_role=body.material_role,
        task_type=body.task_type,
        language=body.language,
        raw_hash=hashlib.sha256(body_bytes).hexdigest(),
        raw_size_bytes=len(body_bytes),
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
        material_id=document.id,
        source_type=body.source_type,
        source_url=s3_url,
        duration_sec=duration_sec,
    )
    # enqueue_ingestion owns the commit (QQ5 helper-owns-commit).

    logger.info(
        "presigned_upload_confirmed",
        document_id=str(document.id),
        node_id=str(node_id),
        key=body.key,
        job_id=str(job.id),
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id
    response.processing_estimate = _processing_estimate()
    return response


@router.get("/nodes/{node_id}/documents")
async def list_documents(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[AuthoredDocumentResponse]:
    """List all authored documents attached to a tree node.

    Returns documents ordered by their position (``order`` field).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = AuthoredDocumentRepository(session)
    documents = await repo.get_for_node(node_id)
    return [AuthoredDocumentResponse.model_validate(d) for d in documents]


@router.post("/documents/{document_id}/base", status_code=202)
async def attach_project_base(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    file: Annotated[
        UploadFile,
        File(description="Base project archive (.zip / .tar.gz / .tgz / .gz)."),
    ],
) -> ProjectBaseAttachResponse:
    """Attach a base archive to a project task (KD18 P2).

    The document must be ``task_type='project'`` (else 422). The archive is
    stored raw to S3 and a new append-only ``ProjectBase`` version is created
    (``pending``) + a deterministic normalization job enqueued — 202, the
    normalization runs asynchronously.

    Security context (Decision 1, 1A): the mandatory structural sandbox is
    ``extract_archive_safely`` (classify) inside ``normalize_archive``, run by
    the ARQ job — the fail-closed ``run_stage1`` archive branch is deliberately
    NOT on this path (it would reject a real project's non-allowlisted files).
    The HTTP gate here is light: an archive-kind extension + a streamed size cap.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )
    if document.task_type != AssignmentType.PROJECT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NOT_A_PROJECT_TASK",
                "details": (
                    "base archives attach only to task_type='project' documents"
                ),
            },
        )

    archive_kind = archive_kind_for_filename(file.filename or "")
    if archive_kind is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NOT_AN_ARCHIVE",
                "details": "base must be a .zip / .tar.gz / .tgz / .gz archive",
            },
        )

    raw = await _read_upload_capped(file, _BASE_ARCHIVE_MAX_UPLOAD_BYTES)

    # UUID-scoped key (matches the existing presigned convention; the version
    # lives in the DB, not the key — avoids a version-in-key S3-overwrite race).
    # The extension is canonicalized to the resolved kind so the worker
    # re-derives the SAME archive_kind via archive_kind_for_filename.
    ext = "tar.gz" if archive_kind == "tar.gz" else "zip"
    archive_key = (
        f"tenants/{tenant.tenant_id}/nodes/{document.course_node_id}/"
        f"bases/{document_id}/{uuid.uuid4()}/original.{ext}"
    )
    await s3.upload_file(archive_key, raw, "application/octet-stream")

    try:
        base = await enqueue_base_normalize(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            authored_document_id=document_id,
            archive_key=archive_key,
        )
    except IntegrityError as exc:
        # A concurrent re-upload won the UNIQUE(doc, version) race. The stored
        # S3 object is orphaned (uuid-scoped — no overwrite of the winner);
        # cleanup folds into the deferred base-S3-cleanup debt (sibling DD-6-R).
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BASE_VERSION_CONFLICT",
                "details": "a concurrent base upload won the version race; retry",
            },
        ) from exc

    logger.info(
        "base_attached",
        document_id=str(document_id),
        base_version_id=str(base.id),
        version=base.version,
    )
    return ProjectBaseAttachResponse(
        base_version_id=base.id,
        version=base.version,
        state=base.state,
    )


@router.get("/documents/{document_id}/base")
async def get_project_base_state(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> ProjectBaseStateResponse:
    """The latest base version's state for author monitoring (KD18 P6).

    Returns the LATEST version REGARDLESS of state (``get_latest`` — NOT
    ``get_latest_ready``) so a freshly re-uploaded ``pending`` / ``failed`` v2
    is not masked by an older READY v1: the author is monitoring THIS upload's
    normalization, the opposite goal of the mode-1 student descriptor
    (``GET /homework/tasks/{id}/base``, which needs a usable READY base). This
    is the only author-facing surface carrying ``failure_reason`` — the manifest
    route is READY-only, so ``pending`` / ``failed`` states render from here.
    404 if the document has no base at all.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )
    base = await ProjectBaseRepository(session).get_latest(document.id)
    if base is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NO_BASE_ATTACHED",
                "details": "no base archive is attached to this task",
            },
        )
    return ProjectBaseStateResponse.model_validate(base)


@router.get("/documents/{document_id}/base/manifest")
async def get_project_base_manifest(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> ProjectBaseManifestResponse:
    """The latest READY base version's manifest (KD18 P2, author-only).

    Returns the algorithmic manifest exactly as the base-normalize worker stored
    it (``schema`` / ``aggregate_hash`` / ``included`` / ``excluded`` /
    ``total_files`` / ``total_bytes``). ``PrepDep`` (author) — NOT the mode-1
    API-key ``GET /base``, because the manifest is the full file listing. 404 if
    the document has no READY base (a pending / failed version carries no
    manifest).

    DD-6-V: typed as the P1 ``Manifest`` dataclass (reconstructed from JSONB via
    the reviver) instead of an opaque ``dict[str, Any]`` — a single source of
    truth for the FE consumer (P6). The wire-shape is unchanged: FastAPI
    serializes the frozen dataclass byte-identically to the stored JSONB.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )
    base = await ProjectBaseRepository(session).get_latest_ready(document.id)
    if base is None or base.manifest is None:
        raise HTTPException(
            status_code=404, detail="No ready base manifest for this document."
        )
    return manifest_from_jsonb(base.manifest)


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> AuthoredDocumentResponse:
    """Get a single authored document by ID.

    Verified through the node → tenant chain.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )
    return AuthoredDocumentResponse.model_validate(document)


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: uuid.UUID,
    body: AuthoredDocumentUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> AuthoredDocumentResponse:
    """Update document metadata (material_role and/or task_type).

    Only fields explicitly sent in the request body are updated.
    Pass ``task_type: null`` to clear the task flag; omit the field
    to keep the current value.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    fields_set = body.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=422,
            detail="At least one field must be provided.",
        )

    if "material_role" in fields_set and body.material_role is not None:
        document = await document_repo.update_material_role(
            document, material_role=body.material_role
        )

    if "task_type" in fields_set:
        document = await document_repo.update_task_type(
            document, task_type=body.task_type
        )

    await session.commit()

    logger.info(
        "document_updated",
        document_id=str(document_id),
        material_role=body.material_role,
        task_type=body.task_type,
        fields=list(fields_set),
    )
    return AuthoredDocumentResponse.model_validate(document)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
) -> None:
    """KD3 soft-delete the document and any descendants (vision §3 KD3).

    Cascade flow per Phase 1 KD3 adoption — mirrors commit (k)
    ``delete_node`` shape with the cascade rooted at AuthoredDocument
    instead of CourseNode:

    1. Verify the document exists and belongs to the caller's tenant
       (404 otherwise — same shape as the rest of the route).
    2. Extract the S3 key from ``document.source_url`` BEFORE cascade
       fires. Order matters: the cascade engine dispatches
       ``scrub_authored_document`` which sets ``source_url = ''`` —
       collecting the key after cascade would miss it.
    3. Issue ``CascadeDeleteService.soft_delete_with_cascade`` rooted
       at the document. The engine drives the four-phase hook chain
       (cancel → invalidate → scrub → write deleted_at → flush) plus
       per-victim scrub dispatch:

       * ``on_cancel_jobs`` — direct binding to
         :class:`JobCancellationService.cancel_jobs_for_entities`
         (L1b). The cascade victim list carries the document's own id,
         which IS the job subject, and JCS now keys on
         ``Job.subject_id IN victim_ids`` — so the document's ingestion
         job is cancelled precisely, with no over-cancel of sibling
         jobs and no course_node_id augmentation. KD13 cancel semantics:
         ``status='cancelled'`` + ``completed_at = now()``;
         ``deleted_at`` REMAINS NULL — Job is subject-cancelled, not
         cascade soft-deleted (per PHASE.md §1.2 audit: Job ∉ any
         ``__cascades_soft_delete_to__`` chain).
       * ``on_invalidate_hashes`` — Gap 3 hook bridging
         :class:`ContentHashService.invalidate_subtree` so parent-
         CourseNode ``content_hash`` recompute treats the document as
         already gone. Closes a tangential gap in commit (l) where
         this hook was omitted entirely (cf. ``nodes.py`` +
         ``storage.py`` mirror sites — hotfix-5 ratified per rule #13
         atomicity).
       * Class-level ``__scrub_callable__`` (AuthoredDocument →
         ``scrub_authored_document`` emitting KD3 marker per
         hotfix-4) clears ``filename`` + ``source_url``.
         DocumentSummary + DocumentSegment descendants are no-op in
         Phase 1 — Amendment 16 defers their scrub callables to
         Phase 2.x and pipeline does not write to these tables yet,
         so cascade traversal through them yields zero victims.
    4. Hand off to ``enqueue_s3_cleanup`` — helper persists a
       ``s3_cleanup`` Job row, commits cascade + Job atomically
       (QQ5 boundary), then dispatches the ARQ task post-commit and
       records the resolved ``arq_job_id``. Empty key list (external
       URL or already-scrubbed source_url) short-circuits — no Job
       row, no ARQ enqueue.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    # (2) Capture the S3 key before cascade scrub blanks ``source_url``.
    # ``extract_key`` returns ``None`` for external URLs that don't
    # belong to our bucket — those rows have no S3 cleanup work.
    course_node_id = document.course_node_id
    file_keys: list[str] = []
    s3_key = s3.extract_key(document.source_url)
    if s3_key is not None:
        file_keys.append(s3_key)
    # Persisted per-slide WebP renders (Phase 6 T3) are S3 objects distinct
    # from source_url; gather them before the cascade scrub so they are
    # scrubbed alongside the document. Already-S3 keys (stored verbatim by
    # the ingest seam) — no extract_key round-trip needed.
    if document.slide_keys:
        file_keys.extend(document.slide_keys)

    # (3) Cascade soft-delete. Class-level ``__scrub_callable__``
    # dispatch (models-fix-3) clears KD3 fields on the AuthoredDocument
    # in the same flush as the ``deleted_at`` write. Phase 1 cascade
    # map for AuthoredDocument resolves to [DocumentSummary,
    # DocumentSegment] — both empty in Phase 1 per Amendment 16.
    # Hook chain per cascade engine ordering invariant: cancel →
    # invalidate → scrub → write deleted_at → flush. L1b: cancel binds
    # ``cancel_jobs_for_entities`` directly — the cascade victim set already
    # carries the document's own id, which IS the job subject, so no
    # course_node_id augmentation is needed (nor wanted: injecting it caused
    # the F3 over-cancel that hit every sibling job of the node).
    cascade_service = CascadeDeleteService(session)
    cascade_map = build_cascade_map(AuthoredDocument)
    content_hash_service = ContentHashService(session)
    job_cancellation_service = JobCancellationService(session)

    async def invalidate_hook(
        ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
    ) -> None:
        await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

    await cascade_service.soft_delete_with_cascade(
        document,
        cascade_map,
        on_cancel_jobs=job_cancellation_service.cancel_jobs_for_entities,
        on_invalidate_hashes=invalidate_hook,
    )

    # (4) Persist the s3_cleanup Job + dispatch ARQ task. Helper owns
    # the QQ5 commit boundary; we pass ``course_node_id`` so tenant
    # scope on the Job row is recoverable via the
    # ``Job.course_node_id → CourseNode.tenant_id`` join even after
    # the cascade scrub clears the document's content fields.
    s3_files_cleaned = len(file_keys)
    if file_keys:
        await enqueue_s3_cleanup(
            session=session,
            arq=arq,
            file_keys=file_keys,
            tenant_id=tenant.tenant_id,
            course_node_id=course_node_id,
        )
    else:
        # No S3 key (external URL or already scrubbed) — still need to
        # commit the cascade soft-delete since the helper would have
        # done it for us otherwise.
        await session.commit()

    logger.info(
        "document_deleted",
        document_id=str(document_id),
        course_node_id=str(course_node_id),
        s3_files_cleaned=s3_files_cleaned,
    )


@router.post("/documents/{document_id}/retry")
async def retry_document(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
    force: bool = False,
) -> AuthoredDocumentCreateResponse:
    """Retry ingestion for a document.

    By default only documents in ``error`` state can be retried.
    Pass ``?force=true`` to re-ingest from any state (e.g. to
    reprocess a ``ready`` document after pipeline improvements).
    Returns 409 if the document is not retryable without ``force``.
    Returns 410 Gone (vision §6 QQ6) if the document is soft-deleted —
    retry on a soft-deleted target is permanently unavailable, never
    transient.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    # QQ6: HTTP 410 Gone on retry of soft-deleted target. Fires BEFORE
    # the state check so a soft-deleted ``error``-state row also yields
    # 410, not 200 (the row's been logically removed; re-ingestion is
    # not a recovery path).
    if document.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Document has been deleted; retry is no longer available.",
        )

    if not force and document.state != "error":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry: document is in '{document.state}' state, "
                f"expected 'error'. Use ?force=true to re-ingest."
            ),
        )

    # Clear error and re-enqueue
    document.error_message = None
    await session.flush()

    # L1b R1 ("do instead of what's there"): supersede any in-flight job of
    # THIS subject (the document) before queuing the new one — otherwise the
    # new Job collides on uq_jobs_subject_in_flight. Cancellation keys on
    # subject_id, so only this document's live job is terminated, never a
    # sibling's. A no-op on the common error-retry (the prior job already
    # failed) and on ``force`` over a ``ready`` doc with no live job.
    jcs = JobCancellationService(session)
    await jcs.cancel_jobs_for_entities([document.id])

    # DD-3.3c-I-B: reuse the duration the original create/confirm probed,
    # rather than re-running the (network-heavy for URL) intake probe. None
    # for legacy documents with no prior probed job — a mild under-count.
    job_repo = JobRepository(session)
    duration_sec = await job_repo.get_latest_ingest_duration_for_material(document.id)

    try:
        job = await enqueue_ingestion(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            node_id=document.course_node_id,
            material_id=document.id,
            source_type=document.source_type,
            source_url=document.source_url,
            duration_sec=duration_sec,
        )
    except IntegrityError as exc:
        # A concurrent retry won the subject slot between the cancel above and
        # this insert — the index is the final judge (invariant 4). Surface a
        # human 409, never a 500 from the index (invariant 5).
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUBJECT_ALREADY_PROCESSING",
                "details": (
                    "a concurrent retry is already re-processing this "
                    "document; try again shortly."
                ),
            },
        ) from exc
    # enqueue_ingestion flipped the document to PENDING and owns the commit
    # (QQ5 helper-owns-commit) — the error_message reset + Job are already
    # durable before the ARQ dispatch.

    logger.info(
        "document_retry",
        document_id=str(document_id),
        job_id=str(job.id),
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id
    response.processing_estimate = _processing_estimate()
    return response
