"""The author's three doors to a test's answer key (mentor-rebuild task 06).

Read the key and its explanations, replace the key whole, clear it. No screen
calls them yet — the author uses an HTTP client — and that is the point of
shipping them as the FINAL routes rather than as a temporary shape: the second
echelon's panel calls these same three, and nothing about them is provisional
(``TASK.md``, decision 1).

Everything these routes decide is decided in
:class:`~course_supporter.homework.reference_service.ReferenceService`; what
lives here is HTTP: who may knock (``PrepDep`` — the author's scope, and only
it), whose task it is (the same tenant guard the base-archive routes use), and
how a refusal becomes a status code.

Why the refusals are codes rather than sentences: the surface picks its own
words for the author, and one reason may need different words in different
places (``language-rules.md``). The code crosses the boundary; the wording does
not.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import (
    ReferenceKeyUpdateRequest,
    ReferenceViewResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.reference_service import (
    ReferenceRefusedError,
    ReferenceService,
    ReferenceView,
)
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository

logger = structlog.get_logger()

router = APIRouter(tags=["references"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

_DOCUMENT_NOT_FOUND: Final[str] = "Document not found"
"""The one answer for a task that is not there and a task that is not yours.

Byte-identical on purpose, and in two directions. Within these routes, a
distinguishable 404 would let a caller with a valid key of one tenant enumerate
the document ids of another (``impl-rules#9``). Across routes, it is the very
string ``api/routes/documents.py`` answers with — a caller cannot learn from
the shape of a refusal which door it knocked on. A test holds the second
direction, because prose claiming it once claimed it wrongly: this constant
carried a trailing full stop the sibling does not have.
"""


async def _require_tenant_task(
    session: AsyncSession, document_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Refuse, identically, a task that does not exist or is not this tenant's."""
    document = await AuthoredDocumentRepository(session).get_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=_DOCUMENT_NOT_FOUND)
    node = await CourseNodeRepository(session).get_by_id(document.course_node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=_DOCUMENT_NOT_FOUND)


def _service(
    session: AsyncSession, arq: ArqRedis, tenant: TenantContext
) -> ReferenceService:
    """The service with the shipped queue behind it."""
    return ReferenceService(
        session,
        ArqExplanationQueue(redis=arq, session=session, tenant_id=tenant.tenant_id),
    )


def _refusal(exc: ReferenceRefusedError) -> HTTPException:
    """Turn a service refusal into the 422 the author reads.

    422 for every one of them, and the code is what distinguishes them: four
    say something about the TASK and one about the key, and the surface sends
    the author to a different place for each.
    """
    return HTTPException(
        status_code=422,
        detail={"code": exc.code.value, "details": exc.details},
    )


@router.get("/documents/{document_id}/reference")
async def get_reference(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> ReferenceViewResponse:
    """The state of this task's key, its answers and its explanations.

    A task with no key answers **200** with ``status = "awaiting_key"``, not
    404: the absence of a key is a state of the task, and an author who has not
    written one yet has not asked for something missing.

    The read is not free of consequences, and that is deliberate (the lazy path
    of ``reference_service``): if the test text changed and its question
    numbers did not, this call carries the author's answers onto the new
    version and asks for their explanations.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await _service(session, arq, tenant).read(document_id)
    except ReferenceRefusedError as exc:
        raise _refusal(exc) from exc
    await session.commit()
    return _response(view)


@router.put("/documents/{document_id}/reference/override")
async def put_reference_override(
    document_id: uuid.UUID,
    body: ReferenceKeyUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> ReferenceViewResponse:
    """Replace the author's key whole; generate its explanations once.

    The last replacement wins — an upsert, not a conflict (ratified
    2026-09-19). The response carries the stored layer, so the author sees what
    actually stands rather than what they sent.

    Sending the same answers again costs nothing: the version for that key
    already exists, so no new work is asked for, and the response reports its
    current state.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await _service(session, arq, tenant).replace_key(
            document_id, body.answers, body.author_explanations
        )
    except ReferenceRefusedError as exc:
        raise _refusal(exc) from exc
    await session.commit()
    logger.info(
        "reference_key_replaced",
        document_id=str(document_id),
        questions=len(body.answers),
        status=view.status.value,
    )
    return _response(view)


@router.delete("/documents/{document_id}/reference/override")
async def delete_reference_override(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> ReferenceViewResponse:
    """Clear the author's key; the task goes back to awaiting one.

    **200**, not 204, and with the same body the other two routes return: the
    author's next question after clearing a key is "what is the state now", and
    answering it here saves a second call. Clearing a task that has no key is
    not an error — the state it asks for is the state that results.

    The generated versions stay. They cost money and they belong to the keys
    that produced them, so sending the same key again finds its explanations
    waiting rather than paying for them twice.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    service = _service(session, arq, tenant)
    try:
        await service.clear_key(document_id)
        view = await service.read(document_id)
    except ReferenceRefusedError as exc:
        raise _refusal(exc) from exc
    await session.commit()
    logger.info("reference_key_cleared", document_id=str(document_id))
    return _response(view)


def _response(view: ReferenceView) -> ReferenceViewResponse:
    """Project the service's view onto the wire shape."""
    return ReferenceViewResponse(
        status=view.status.value,
        version=view.version,
        answers=view.answers,
        explanations=view.explanations,
        carried_over=view.carried_over,
        language=view.language,
        failure_reason=view.failure_reason,
    )
