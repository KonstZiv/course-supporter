"""Prices before an action (BIL-E, part B): the estimate routes.

Routes
------
- ``POST /portal/tasks/{authored_document_id}/estimate`` — a student's
  session: the price of a review of this task, without a file (``typical``)
  or with the file about to be sent (``file``; read by the doors, never
  stored, no submission is made).
- ``POST /nodes/{node_id}/documents/estimate`` — an author (the tenant's key,
  scope ``prep``): the price of processing a material of the node, from its
  features as the client knows them (kind, duration, slides, text size).
- ``POST /documents/{document_id}/reference/estimate`` — an author (scope
  ``prep``): the price of the explanations of a test's key.

Each answers with the price in units, what is available, whether it is
enough and how much is missing, and whether charging is on
(:mod:`course_supporter.billing.quotes`). The refusal of a submission for want
of units is 402 ``{"detail": {"code": "insufficient_units", "details":
{price_units, available_units, missing_units}}}``
(:func:`course_supporter.billing.quotes.require_enough`).

A student's review the author pays for under a rule carries no numbers
(``price_units``, ``available_units``, ``missing_units`` null, ``enough``
true). A review whose price could not be estimated answers 200 with
``basis: "ceiling"`` and ``price_units`` = N, the most it can cost (BIL-E2,
4a); only a file the doors refuse is 422.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.routes.portal_submissions import _answerable_task
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.billing.estimates import preparation_e_usd, processing_e_usd
from course_supporter.billing.kinds import ActionKind
from course_supporter.billing.quotes import Quote, author_quote, review_quote
from course_supporter.cost_estimate.actions import MaterialFactors
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
)

router = APIRouter(tags=["billing"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]

_NOT_FOUND = "Not found."


class AuthorEstimateResponse(BaseModel):
    """The price of an author's action and whether it may start now."""

    price_units: int = Field(description="P: the price shown, in units.")
    available_units: int | None = Field(
        description="What the paying account can spend now."
    )
    enough: bool = Field(
        description="May the action start now: available >= P, or no threshold "
        "applies (credit allowed), or charging is off."
    )
    missing_units: int | None = Field(description="P - available when not enough.")
    charging_enabled: bool = Field(
        description="BILLING_CHARGING_ENABLED: while false nothing is refused."
    )


class ReviewEstimateResponse(BaseModel):
    """The price of a student's review; the author's numbers never shown."""

    price_units: int | None = Field(
        description="P: the price shown, in units; at basis ceiling, N — the "
        "most the review can cost (the fact is charged). Null when the author "
        "pays."
    )
    available_units: int | None = Field(
        description="The student's available units; null when the author pays."
    )
    enough: bool = Field(
        description="May the submission be sent now: available >= P (N at the "
        "ceiling), or the author pays (always — whether the review starts is "
        "decided when it runs), or charging is off."
    )
    missing_units: int | None = Field(
        description="P - available when not enough; null when the author pays."
    )
    charging_enabled: bool = Field(
        description="Charging on AND the task's path not in its test mode."
    )
    basis: Literal["typical", "file", "ceiling"] = Field(
        description="typical: this task's usual size; file: the file sent; "
        "ceiling: the price could not be estimated — price_units is the most "
        "the review can cost."
    )
    paid_by: Literal["self", "author"] = Field(
        description="self: the student; author: the author under a payment rule."
    )


class ProcessingEstimateRequest(BaseModel):
    """What the client knows of a material before it is processed."""

    kind: Literal["text", "web", "code", "audio", "video", "presentation"]
    duration_sec: float | None = Field(default=None, ge=0)
    slides: int | None = Field(default=None, ge=0)
    text_chars: int | None = Field(default=None, ge=0)
    files: int | None = Field(default=None, ge=0)


def _review_response(quote: Quote) -> ReviewEstimateResponse:
    assert quote.basis is not None and quote.paid_by is not None
    return ReviewEstimateResponse(
        price_units=quote.price_units,
        available_units=quote.available_units,
        enough=quote.enough,
        missing_units=quote.missing_units,
        basis=quote.basis,
        paid_by=quote.paid_by,
        charging_enabled=quote.charging_enabled,
    )


def _author_response(quote: Quote) -> AuthorEstimateResponse:
    return AuthorEstimateResponse(
        price_units=quote.price_units,
        available_units=quote.available_units,
        enough=quote.enough,
        missing_units=quote.missing_units,
        charging_enabled=quote.charging_enabled,
    )


async def read_for_quote(
    session: AsyncSession,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    *,
    filename: str,
    data: bytes,
) -> Any:
    """The doors' reading of a file about to be sent, as the path will read it.

    Only a file-answered type that is not a project is read (a test is answered
    with its answers, a project is not charged — S2). Raises
    :class:`~course_supporter.security.exceptions.SecurityRejectedError` when
    the doors would refuse the file.
    """
    from course_supporter.homework.path_runner import door_languages, read_text_work

    if task.task_type in (AssignmentType.TEST.value, AssignmentType.PROJECT.value):
        return None
    probe = HomeworkSubmission(
        student_id=student_id, authored_document_id=task.id, response_language=None
    )
    languages = await door_languages(session, probe)
    return read_text_work(
        filename=filename, file_bytes=data, languages=languages.verify, note_flags=()
    )


@dataclass(frozen=True, slots=True)
class FileForQuote:
    """A file about to be sent, as the price reads it."""

    reading: Any | None
    """The doors' reading (:func:`read_for_quote`); ``None`` — not read."""
    error: str | None = None
    """The class of the error that broke the reading: the price falls to the
    ceiling (BIL-E2, 4a)."""


async def file_for_quote(
    session: AsyncSession,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    *,
    filename: str,
    data: bytes,
) -> FileForQuote:
    """:func:`read_for_quote`, any failure but the doors' own kept as an error.

    Raises :class:`~course_supporter.security.exceptions.SecurityRejectedError`
    when the doors refuse the file — the file's problem, not the price's. The
    reads run in a savepoint, so a failed one leaves the session usable.
    """
    from course_supporter.security.exceptions import SecurityRejectedError

    try:
        async with session.begin_nested():
            reading = await read_for_quote(
                session, task, student_id, filename=filename, data=data
            )
    except SecurityRejectedError:
        raise
    except Exception as exc:
        return FileForQuote(None, type(exc).__name__)
    return FileForQuote(reading)


@router.post(
    "/portal/tasks/{authored_document_id}/estimate",
    response_model=ReviewEstimateResponse,
)
async def estimate_portal_review(
    student: StudentDep,
    session: SessionDep,
    authored_document_id: Annotated[
        uuid.UUID, Path(description="The task (AuthoredDocument) to be answered.")
    ],
    file: Annotated[
        UploadFile | None,
        File(description="The file about to be sent; omitted — the typical price."),
    ] = None,
) -> ReviewEstimateResponse:
    """The price of a review of this task before it is sent; nothing is stored.

    The access gates of a submission (one 404). A file the doors refuse is 422
    with the doors' reason, as the submission would be.
    """
    from course_supporter.security.exceptions import SecurityRejectedError

    task = await _answerable_task(session, student, authored_document_id)
    sent = FileForQuote(None)
    if file is not None:
        data = await file.read()
        try:
            sent = await file_for_quote(
                session,
                task,
                student.student_id,
                filename=file.filename or "work",
                data=data,
            )
        except SecurityRejectedError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.category.value, "details": str(exc)},
            ) from exc
    quote = await review_quote(
        session,
        task=task,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
        reading=sent.reading,
        read_error=sent.error,
    )
    if file is not None and quote.basis == "typical":
        # A file the price does not read (a test's answers, a project): it was
        # still sent, so the basis says so.
        quote = replace(quote, basis="file")
    await session.commit()  # accounts are created lazily on the way
    return _review_response(quote)


async def _course_root_of_node(
    session: AsyncSession, tenant_id: uuid.UUID, node_id: uuid.UUID
) -> uuid.UUID:
    node = await session.get(CourseNode, node_id)
    if node is None or node.tenant_id != tenant_id or node.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    from course_supporter.storage.course_node_repository import CourseNodeRepository

    root = await CourseNodeRepository(session).get_root_for(node_id)
    return root.id if root is not None else node_id


@router.post(
    "/nodes/{node_id}/documents/estimate",
    response_model=AuthorEstimateResponse,
)
async def estimate_processing(
    tenant: PrepDep,
    session: SessionDep,
    node_id: uuid.UUID,
    body: ProcessingEstimateRequest,
) -> AuthorEstimateResponse:
    """The price of processing a material of this node, from its features.

    Priced by the estimate's own function (``estimate_processing``) on what the
    client sends; the real price is fixed in the worker, when the material is
    read. Missing features the material needs give a price of 0.
    """
    root_id = await _course_root_of_node(session, tenant.tenant_id, node_id)
    factors = MaterialFactors(
        duration_sec=body.duration_sec,
        slides=body.slides,
        files=body.files,
        text_chars=body.text_chars,
        safety_chars=body.text_chars,
    )
    quote = await author_quote(
        session,
        action_kind=ActionKind.PROCESSING,
        tenant_id=tenant.tenant_id,
        course_node_id=root_id,
        e_usd=processing_e_usd(kind=body.kind, factors=factors),
    )
    await session.commit()
    return _author_response(quote)


@router.post(
    "/documents/{document_id}/reference/estimate",
    response_model=AuthorEstimateResponse,
)
async def estimate_key_explanation(
    tenant: PrepDep,
    session: SessionDep,
    document_id: uuid.UUID,
) -> AuthorEstimateResponse:
    """The price of the explanations of this test's key, asked by the author."""
    from course_supporter.homework.task_context import load_task_context

    task = await session.get(AuthoredDocument, document_id)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    root_id = await _course_root_of_node(session, tenant.tenant_id, task.course_node_id)
    title, description, text = await load_task_context(session, document_id)
    chars = sum(len(part or "") for part in (title, description, text))
    quote = await author_quote(
        session,
        action_kind=ActionKind.TASK_PREPARATION,
        tenant_id=tenant.tenant_id,
        course_node_id=root_id,
        e_usd=preparation_e_usd(kind="key_explanation", task_chars=chars),
    )
    await session.commit()
    return _author_response(quote)
