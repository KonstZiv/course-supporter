"""The author's three doors to a task's criteria list (mentor-rebuild task 08).

Read the list — the model's, the author's edit, the one in force and the
contradictions the model found — replace the edit whole, reset it. As with a
test's key (``api/routes/references.py``), no screen calls them yet: the author
uses an HTTP client, and the second echelon's panel will call these same three
(``03-BINDING.md`` 2.3, clarified 2026-09-19), so they ship as the final
routes.

What the routes answer is decided in
:class:`~course_supporter.homework.criteria_edit_service.CriteriaEditService`;
what lives here is HTTP: who may knock (``PrepDep`` — the author's scope, and
only it), whose task it is, and how a refusal becomes a status code.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    CriteriaInForceResponse,
    CriteriaOverrideRequest,
    CriteriaViewResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.criteria_edit_service import (
    CriteriaEditService,
    CriteriaRefusedError,
    CriteriaView,
)
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository

logger = structlog.get_logger()

router = APIRouter(tags=["criteria"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

_DOCUMENT_NOT_FOUND: Final[str] = "Document not found"
"""The one answer for a task that is not there and for a task that is not yours.

The string the document routes and the key's routes answer with too, so a
caller learns nothing from which door refused it — a test holds it, not this
sentence.
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

    A task whose list is not composed yet answers **200** with
    ``status = "awaiting_first_submission"`` and a message saying when it will
    be, not 404: no route composes a list — the first submission of a
    student's work does (``TASK.md`` section 9, decision 1). Reading writes
    nothing.
    """
    await _require_tenant_task(session, document_id, tenant.tenant_id)
    try:
        view = await CriteriaEditService(session).read(document_id)
    except CriteriaRefusedError as exc:
        raise _refusal(exc) from exc
    return _response(view)


@router.put("/documents/{document_id}/criteria/override")
async def put_criteria_override(
    document_id: uuid.UUID,
    body: CriteriaOverrideRequest,
    tenant: PrepDep,
    session: SessionDep,
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
    return _response(view)


@router.delete("/documents/{document_id}/criteria/override")
async def delete_criteria_override(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
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
    return _response(view)


def _response(view: CriteriaView) -> CriteriaViewResponse:
    """Project the service's view onto the wire shape."""
    in_force = view.in_force
    return CriteriaViewResponse(
        status=view.status,
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
