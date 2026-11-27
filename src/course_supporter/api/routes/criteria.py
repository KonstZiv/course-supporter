"""The author's four doors to a task's criteria list (mentor-rebuild task 08, DD-SP-DR).

Read the list — the model's, the author's edit, the one in force and the
contradictions the model found — replace the edit whole, reset it, and ask for
it to be composed before any student's submission does it. The author panel's
criteria screen (course-supporter-ui, ``CriteriaEditorPage``) calls these same
four; an author may also use an HTTP client.

What the routes answer is decided in
:class:`~course_supporter.homework.criteria_edit_service.CriteriaEditService`;
what lives here is HTTP: who may knock (``PrepDep`` — the author's scope, and
only it), whose task it is, and how a refusal becomes a status code.

The fourth door composes nothing itself: it queues one ``criteria_composition``
job, whose worker (``workers/criteria_compose.py``) calls the same
``CriteriaListService`` a student's first submission calls, so the rules and the
charge (``task_preparation``) are one. The author may use it or not; without it
the first submission composes the list, as before.

A write that leaves a list in force puts back in the queue the students' works
of the task that wait for one (task 09b, ``PRE-FLIGHT.md`` 9.2, entry 2) —
after the write is committed, so a continuation reads the list it was queued
for. A composition does the same, in its worker, when it has stored the list.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_session
from course_supporter.api.schemas import (
    CriteriaInForceResponse,
    CriteriaOverrideRequest,
    CriteriaViewResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_criteria_composition
from course_supporter.homework.criteria_edit_service import (
    ComposeAction,
    CriteriaEditService,
    CriteriaRefusedError,
    CriteriaView,
)
from course_supporter.homework.path_continuation import resume_awaiting_criteria
from course_supporter.homework.reference_service import GenerationInProgressError
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository

logger = structlog.get_logger()

router = APIRouter(tags=["criteria"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

_DOCUMENT_NOT_FOUND: Final[str] = "Document not found"
"""The one answer for a task that is not there and for a task that is not yours.

The string the document routes and the key's routes answer with too, so a
caller learns nothing from which door refused it — a test holds it, not this
sentence.
"""


_TASK_BUSY_DETAILS: Final[str] = (
    "Завдання ще обробляється — спробуйте скласти перелік, коли обробку буде завершено"
)
"""What the author reads when another job of the task holds its one slot.

Ukrainian, as the operator set it (2026-11-27); the code beside it, the one the
key's routes use for the same collision, is what a surface picks its own words
by.
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


async def _continue_the_waiting(
    session: AsyncSession,
    arq: ArqRedis,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    view: CriteriaView,
) -> None:
    """Put back the works that wait for the list this write left in force.

    Best-effort: the author's write is committed and stays so whatever the
    queue says — the worker's start finds whatever this misses.
    """
    if view.in_force is None or view.waiting_submissions == 0:
        return
    try:
        await resume_awaiting_criteria(
            session, arq, tenant_id=tenant_id, authored_document_id=document_id
        )
    except Exception:
        logger.warning(
            "criteria_waiting_continuation_failed",
            document_id=str(document_id),
            exc_info=True,
        )


def _task_busy(document_id: uuid.UUID) -> HTTPException:
    """The 409 for a task whose one job slot is taken; the author reads why."""
    return HTTPException(
        status_code=409,
        detail={
            "code": GenerationInProgressError(document_id).code,
            "details": _TASK_BUSY_DETAILS,
        },
    )


def _refusal(exc: CriteriaRefusedError) -> HTTPException:
    """Turn a service refusal into the 422 the author reads; the code says which."""
    return HTTPException(
        status_code=422,
        detail={"code": exc.code.value, "details": exc.details},
    )


@router.get("/documents/{document_id}/criteria")
async def get_criteria(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
) -> CriteriaViewResponse:
    """The criteria of the task's current version: each layer apart.

    A task whose list is not composed yet answers **200**, not 404: this route
    composes nothing. The list is composed by the first submission of a
    student's work (``TASK.md`` section 9, decision 1) or by the author's
    ``POST …/criteria/compose``, whichever asks first. The status says why
    there is none —
    ``awaiting_first_submission``, ``composing`` or ``not_composed`` with its
    ``reason_code`` — beside how many works wait and a message saying what the
    author can do (task 09b). Reading writes nothing.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await CriteriaEditService(session).read(document_id)
    except CriteriaRefusedError as exc:
        raise _refusal(exc) from exc
    return _response(view)


@router.post(
    "/documents/{document_id}/criteria/compose",
    responses={202: {"model": CriteriaViewResponse}},
)
async def compose_criteria(
    document_id: uuid.UUID,
    response: Response,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> CriteriaViewResponse:
    """Compose the task's criteria list now, before any student's submission does.

    The author may use this; without it the first submission composes the list.
    The work is the same one — one composition per task version, charged to the
    tenant as ``task_preparation`` — only earlier.

    * A list is in force: **200** with the body the reading returns; nothing is
      queued.
    * One is being composed already — by a submission, or by an earlier request
      of this one: **202** with the current reading (``status = "composing"``);
      nothing is queued.
    * Otherwise one composition is queued and the answer is **202** with
      ``status = "composing"``. It is truthful at once, not after the worker
      starts: the queued job is a row, and the reading counts it.
    * Another job of the task is in flight — its processing, a key's
      explanations: **409** ``GENERATION_IN_PROGRESS``, nothing queued; the
      request is not wrong, only early.
    * Not a ``short_task``, ``task`` or ``project``, or the task has no ready
      summary yet: the **422** the other routes answer (``NOT_A_TEXT_TASK``,
      ``TASK_NOT_READY``). A foreign or missing task: the same **404**.

    The end of the work is read from ``GET``: ``ready`` with a list in force, or
    ``not_composed`` with its ``reason_code`` when the model's ladder gave up —
    a refusal of funds among them, read as a submission's is.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    service = CriteriaEditService(session)
    try:
        ask = await service.compose_asked(document_id)
    except CriteriaRefusedError as exc:
        raise _refusal(exc) from exc
    if ask.action is ComposeAction.ALREADY_IN_FORCE:
        return _response(ask.view)
    response.status_code = 202
    if ask.action is ComposeAction.UNDER_WAY:
        return _response(ask.view)
    if ask.action is ComposeAction.TASK_BUSY:
        raise _task_busy(document_id)
    try:
        await enqueue_criteria_composition(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            authored_document_id=document_id,
        )
    except IntegrityError as exc:
        # Another job took the task's one slot between the read and the insert.
        # The refused insert voided the transaction; read again to say which.
        await session.rollback()
        ask = await service.compose_asked(document_id)
        if ask.action is ComposeAction.UNDER_WAY:
            return _response(ask.view)
        if ask.action is ComposeAction.ALREADY_IN_FORCE:
            response.status_code = 200
            return _response(ask.view)
        raise _task_busy(document_id) from exc
    logger.info("criteria_composition_requested", document_id=str(document_id))
    return _response((await service.compose_asked(document_id)).view)


@router.put("/documents/{document_id}/criteria/override")
async def put_criteria_override(
    document_id: uuid.UUID,
    body: CriteriaOverrideRequest,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> CriteriaViewResponse:
    """Replace the author's edit whole; it is in force for this task version.

    The last replacement wins, and the edit it replaced stays as a snapshot. A
    criterion or a point sent with its id keeps it; one sent without gets a
    new id, and the response carries what is stored, so the author sees it.
    The edit belongs to the version of the task it is made on: once the task
    changes, reviews no longer use it.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await CriteriaEditService(session).replace(document_id, body.criteria)
    except CriteriaRefusedError as exc:
        raise _refusal(exc) from exc
    await session.commit()
    logger.info(
        "criteria_override_replaced",
        document_id=str(document_id),
        criteria=len(body.criteria),
    )
    await _continue_the_waiting(
        session,
        arq,
        tenant_id=tenant.tenant_id,
        document_id=document_id,
        view=view,
    )
    return _response(view)


@router.delete("/documents/{document_id}/criteria/override")
async def delete_criteria_override(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
) -> CriteriaViewResponse:
    """Reset the author's edit; the model's list, if any, is in force again.

    **200** with the body the other two routes return, as the key's routes do.
    The reset is soft: the edit stays as a snapshot. Resetting a task that has
    no edit is not an error.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await CriteriaEditService(session).reset(document_id)
    except CriteriaRefusedError as exc:
        raise _refusal(exc) from exc
    await session.commit()
    logger.info("criteria_override_reset", document_id=str(document_id))
    await _continue_the_waiting(
        session,
        arq,
        tenant_id=tenant.tenant_id,
        document_id=document_id,
        view=view,
    )
    return _response(view)


def _response(view: CriteriaView) -> CriteriaViewResponse:
    """Project the service's view onto the wire shape."""
    in_force = view.in_force
    return CriteriaViewResponse(
        status=view.status,
        reason_code=view.reason_code,
        waiting_submissions=view.waiting_submissions,
        message=view.message,
        model=list(view.model) if view.model is not None else None,
        author=list(view.author) if view.author is not None else None,
        in_force=None
        if in_force is None
        else CriteriaInForceResponse(
            layer=in_force.layer, criteria=list(in_force.criteria)
        ),
        contradictions=list(view.contradictions),
        concepts=list(view.concepts),
    )
