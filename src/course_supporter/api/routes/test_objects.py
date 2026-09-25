"""The author's routes of a test written in the system (mentor-rebuild task 07b).

Create a test in a node, read and replace its draft, publish it, and take it out
as YAML — the five routes of PRE-FLIGHT section 7.1. Hiding a test is the
document's own ``DELETE`` (decision 17), and reading the state of its
explanations is the key's own ``GET …/reference`` (section 7.3): neither needs a
route here.

What lives here is HTTP: who may knock (``PrepDep`` — the author's scope, and
only it), whose node or test it is (one 404 for what is not there and what is
not yours), how a body is read (JSON or YAML, capped before it is read whole),
and how a refusal becomes a status code. What a test is and how it is published
is decided in :class:`~course_supporter.homework.test_object_service.TestObjectService`;
the rules of the format in :mod:`course_supporter.homework.test_yaml`.

Refusals:
    A draft the format refuses answers ``{"code", "details", "place"}`` — 422,
    or 413 for ``TEST_TOO_LARGE`` (section 6.3). A text a Stage 1 screen refuses
    answers 400 ``SECURITY_REJECTED`` with its category, as a refused upload
    does. A document of this tenant that is not a test written in the system
    answers 422 ``NOT_A_TEST_OBJECT``. A publication that meets another job of
    the test answers 409 ``GENERATION_IN_PROGRESS`` and keeps nothing.

What costs money:
    Only a publication, and only when it asks for explanations (section 8).
    Creating, reading and replacing a draft ask for no work — their service
    holds a queue that refuses to be asked — and taking it out reads the draft
    alone.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.routes._author_shared import generation_in_progress
from course_supporter.api.schemas import (
    WrittenTestDraft,
    WrittenTestOption,
    WrittenTestPublicationResponse,
    WrittenTestQuestion,
    WrittenTestResponse,
    WrittenTestVersion,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.reference_service import (
    GenerationInProgressError,
    ReadOnlyQueue,
)
from course_supporter.homework.test_object import DraftBody, published_form
from course_supporter.homework.test_object_service import TestObjectService
from course_supporter.homework.test_yaml import (
    MAX_BODY_BYTES,
    DraftRefusalCode,
    DraftRefusedError,
    LoadedDraft,
    RefusalPlace,
    dump_test_yaml,
    load_test_json,
    load_test_yaml,
)
from course_supporter.models.source import SourceType
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import AuthoredDocument, CourseNode, TestVersion
from course_supporter.storage.test_object_repository import TestObjectRepository

logger = structlog.get_logger()

router = APIRouter(tags=["tests"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

_DOCUMENT_NOT_FOUND: Final[str] = "Document not found"
"""The answer for a test that is not there and for a test that is not yours.

The string the document routes and the key's routes answer with too, so a
caller learns nothing from which door refused it — a test holds it, not this
sentence.
"""

_NODE_NOT_FOUND: Final[str] = "Node not found"
"""The document routes' answer for a node that is not there or not yours."""

_NOT_A_TEST_OBJECT: Final[str] = "NOT_A_TEST_OBJECT"

_JSON_TYPES: Final = frozenset({"application/json"})
_YAML_TYPES: Final = frozenset({"application/yaml", "text/yaml"})
_YAML_FILENAME: Final = "test.yaml"
"""The name a YAML body is screened under: a request body has none of its own."""


async def _require_node(
    session: AsyncSession, node_id: uuid.UUID, tenant_id: uuid.UUID
) -> CourseNode:
    """The tenant's node, or the one 404 for a node missing or not yours."""
    node = await CourseNodeRepository(session).get_by_id(node_id)
    if node is None or node.deleted_at is not None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=_NODE_NOT_FOUND)
    return node


async def _require_written_test(
    session: AsyncSession, document_id: uuid.UUID, tenant_id: uuid.UUID
) -> AuthoredDocument:
    """The tenant's test written in the system — one 404 for missing or not yours.

    A document of this tenant that is some other material, or a test written as
    a file, is refused with its code: it is there and it is the author's, only
    not a test these routes can read.
    """
    document = await AuthoredDocumentRepository(session).get_by_id(document_id)
    if document is None or document.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_DOCUMENT_NOT_FOUND)
    node = await CourseNodeRepository(session).get_by_id(document.course_node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=_DOCUMENT_NOT_FOUND)
    if document.source_type != SourceType.TEST_OBJECT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _NOT_A_TEST_OBJECT,
                "details": "this document is not a test written in the system",
            },
        )
    return document


async def _course_root_id(
    session: AsyncSession, node: CourseNode, tenant_id: uuid.UUID
) -> uuid.UUID:
    """The root of the course a node is in — the node itself, for a root."""
    if node.parent_id is None:
        return node.id
    root = await CourseNodeRepository(session).get_root_for(
        node.id, tenant_id=tenant_id
    )
    if root is None:
        msg = f"node {node.id} reaches no root of its own tenant"
        raise RuntimeError(msg)
    return root.id


async def _read_draft(
    request: Request, language: str, *, title_required: bool
) -> LoadedDraft:
    """The draft a request carries, as JSON or as YAML, or the refusal it earns."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip()
    media_type = media_type.lower()
    if media_type not in _JSON_TYPES | _YAML_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Send the test as application/json, application/yaml or text/yaml.",
        )
    content = await _read_capped(request)
    try:
        if media_type in _JSON_TYPES:
            return load_test_json(
                content, language=language, title_required=title_required
            )
        return load_test_yaml(
            content,
            language=language,
            filename=_YAML_FILENAME,
            title_required=title_required,
        )
    except DraftRefusedError as exc:
        raise _draft_refused(exc) from exc
    except SecurityRejectedError as exc:
        raise _security_rejected(exc) from exc


async def _read_capped(request: Request) -> bytes:
    """The body, read no further than a test may go (section 6.2).

    A declared length over the cap is refused before a byte is read; the read
    itself stops the moment the running total passes it, since a declared
    length can lie.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _draft_refused(_too_large())
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _draft_refused(_too_large())
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large() -> DraftRefusedError:
    return DraftRefusedError(
        DraftRefusalCode.TEST_TOO_LARGE,
        f"the test is over {MAX_BODY_BYTES} bytes; a test may have {MAX_BODY_BYTES}",
        RefusalPlace(),
    )


def _draft_refused(exc: DraftRefusedError) -> HTTPException:
    """A draft the format refuses: its code, what is wrong, and where (section 6.3)."""
    return HTTPException(
        status_code=413 if exc.code is DraftRefusalCode.TEST_TOO_LARGE else 422,
        detail={
            "code": exc.code.value,
            "details": exc.details,
            "place": exc.place.to_json(),
        },
    )


def _security_rejected(exc: SecurityRejectedError) -> HTTPException:
    """A text a Stage 1 screen refused, answered as a refused upload is."""
    return HTTPException(
        status_code=400,
        detail={
            "code": "SECURITY_REJECTED",
            "category": exc.category.value,
            "details": exc.detail,
        },
    )


async def _view(
    session: AsyncSession, document: AuthoredDocument, language: str
) -> WrittenTestResponse:
    """The test as its author reads it: the draft with its letters, the version."""
    repo = TestObjectRepository(session)
    draft = await repo.get_draft(document.id)
    if draft is None:
        msg = f"test {document.id} has no draft; a test is created with one"
        raise RuntimeError(msg)
    shown = published_form(DraftBody.from_jsonb(draft.body), language)
    published = await repo.latest_version(document.id)
    return WrittenTestResponse(
        id=document.id,
        course_node_id=document.course_node_id,
        title=document.title,
        language=language,
        draft=WrittenTestDraft(
            pass_threshold=shown.pass_threshold,
            questions=[
                WrittenTestQuestion(
                    number=question.number,
                    text=question.text,
                    options=[
                        WrittenTestOption(
                            label=option.label, text=option.text, correct=option.correct
                        )
                        for option in question.options
                    ],
                    explanation=question.explanation,
                )
                for question in shown.questions
            ],
        ),
        published=None if published is None else _version(published),
    )


def _version(published: TestVersion) -> WrittenTestVersion:
    return WrittenTestVersion(
        number=published.version,
        version=published.content_digest,
        published_at=published.published_at,
    )


@router.post("/nodes/{node_id}/tests", status_code=201)
async def create_written_test(
    node_id: uuid.UUID,
    request: Request,
    tenant: PrepDep,
    session: SessionDep,
) -> WrittenTestResponse:
    """Create a test written in the system in a node (task 07b).

    The body is the test of PRE-FLIGHT section 6.1: ``application/json`` for
    the structure, ``application/yaml`` or ``text/yaml`` for its YAML, at most
    256 KiB, and it names the test: without a ``title`` it is refused, 422
    ``TEST_FIELD_INVALID`` at the place the field is missing. **201** with the
    test — its document, its title, its draft with the letters a publication
    would give it, and ``published: null``. Nothing is published and nothing
    is asked for; no student sees the test before its first publication.

    **404** ``Node not found`` for a node that is not there or not this
    tenant's — one body for both. **422** or **413** with
    ``{"code", "details", "place"}`` for a draft the format refuses; **400**
    ``SECURITY_REJECTED`` for a text a Stage 1 screen refuses; **415** for any
    other body.
    """
    node = await _require_node(session, node_id, tenant.tenant_id)
    service = TestObjectService(session, ReadOnlyQueue())
    language = await service.course_language(
        await _course_root_id(session, node, tenant.tenant_id)
    )
    loaded = await _read_draft(request, language, title_required=True)
    if loaded.title is None:
        msg = "a new test was read without the title its loader requires"
        raise RuntimeError(msg)
    document = await service.create(
        node.id, loaded.body, title=loaded.title, language=language
    )
    await session.commit()
    logger.info("written_test_created", document_id=str(document.id))
    return await _view(session, document, language)


@router.get("/tests/{document_id}/draft")
async def read_written_test(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> WrittenTestResponse:
    """The draft of a test written in the system, and its version in force.

    The draft carries the marks the author set and the letters a publication
    would give its options now, in the course's alphabet; ``published`` is the
    version in force — its number, its ``version`` as a student is shown it,
    when it was published — or ``null`` before the first publication.

    **404** ``Document not found`` for a test that is not there or not this
    tenant's — one body for both; **422** ``NOT_A_TEST_OBJECT`` for another
    document of this tenant.
    """
    document = await _require_written_test(session, document_id, tenant.tenant_id)
    language = await TestObjectService(session, ReadOnlyQueue()).course_language(
        document.course_root_id
    )
    return await _view(session, document, language)


@router.put("/tests/{document_id}/draft")
async def replace_written_test_draft(
    document_id: uuid.UUID,
    request: Request,
    tenant: PrepDep,
    session: SessionDep,
) -> WrittenTestResponse:
    """Replace the draft whole, from JSON or YAML; a title given renames the test.

    The draft is always a checked one: a body the format refuses leaves the
    draft as it was. A title is the document's, not the draft's, so it changes
    at once and no publication carries it; a body without one keeps the title.
    Nothing is published and nothing is asked for.

    The refusals of creating a test, and **404** / **422** as for reading one.
    """
    document = await _require_written_test(session, document_id, tenant.tenant_id)
    service = TestObjectService(session, ReadOnlyQueue())
    language = await service.course_language(document.course_root_id)
    loaded = await _read_draft(request, language, title_required=False)
    await service.save_draft(document.id, loaded.body, title=loaded.title)
    await session.commit()
    return await _view(session, document, language)


@router.post("/tests/{document_id}/publish")
async def publish_written_test(
    document_id: uuid.UUID,
    response: Response,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> WrittenTestPublicationResponse:
    """Publish the draft (PRE-FLIGHT section 8).

    **201** with a new version; **200** when the draft equals the version in
    force — the same version. The explanations in the course language are
    asked for when the version's axes are new, and again only when the ones
    asked for before failed or were left stuck (section 8.6).

    **409** ``GENERATION_IN_PROGRESS`` when another job of the test is in
    flight: nothing of the publication is kept, and the author publishes again
    once it ends (decision 12). **404** / **422** as for reading a test.
    """
    await _require_written_test(session, document_id, tenant.tenant_id)
    service = TestObjectService(
        session,
        ArqExplanationQueue(redis=arq, session=session, tenant_id=tenant.tenant_id),
    )
    try:
        publication = await service.publish(document_id)
        await session.commit()
    except GenerationInProgressError as exc:
        await session.rollback()
        raise generation_in_progress(exc) from exc
    response.status_code = 201 if publication.created else 200
    return WrittenTestPublicationResponse(
        created=publication.created, published=_version(publication.version)
    )


@router.get(
    "/tests/{document_id}/yaml",
    response_class=Response,
    responses={200: {"content": {"application/yaml": {}}}},
)
async def export_written_test(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> Response:
    """The draft as canonical YAML — the author's own, and only the author's.

    The key is in it — the marks and the author's explanations — so it is served
    to the author's scope alone; no route of a student or a channel returns it.
    Reading it back gives the same title and draft (section 6.4).

    **404** / **422** as for reading a test.
    """
    document = await _require_written_test(session, document_id, tenant.tenant_id)
    draft = await TestObjectRepository(session).get_draft(document.id)
    if draft is None:
        msg = f"test {document.id} has no draft; a test is created with one"
        raise RuntimeError(msg)
    text = dump_test_yaml(DraftBody.from_jsonb(draft.body), title=document.title)
    return Response(content=text, media_type="application/yaml")
