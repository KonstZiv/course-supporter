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
- ``POST /nodes/{node_id}/summary/estimate`` — an author (scope ``prep``,
  their own node): the price of regenerating the section descriptions of the
  node's subtree (the Methodist, ``POST /nodes/{node_id}/summary/generate``) —
  the author's processing, priced on the job's own factors by the function
  the job reserves on; the same body as the generate route. Every summary
  fresh: ``price_units`` 0, ``unpriced_reason`` ``nothing_to_do``.
- ``POST /documents/{document_id}/reference/estimate`` — an author (scope
  ``prep``): the price of the explanations of a test's key.

They are reads: nothing is stored and no account is created (BIL-E3) — a
payer without one reads as an empty account.

Each answers with the price in units, what is available, whether it is
enough and how much is missing, and whether charging is on
(:mod:`course_supporter.billing.quotes`). An author's action that cannot be
priced (BIL-E3) answers ``price_units`` and ``missing_units`` null, ``enough``
true, and why: ``unpriced_reason`` ``missing_features`` — the body lacks a
feature the kind of material needs (``missing_features`` names the fields) —
or ``estimate_failed``. The action is not refused in advance: its start checks
the threshold on its real features. One action has a price of 0 for a reason:
a regeneration of node summaries that would call no model answers
``price_units`` 0 and ``unpriced_reason`` ``nothing_to_do``. The refusal of a
submission for want of units is 402 ``{"detail": {"code":
"insufficient_units", "details": {price_units, available_units,
missing_units}}}``
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
from course_supporter.api.schemas import NodeSummaryGenerateRequest
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.billing.estimates import (
    key_explanation_task_chars,
    node_summary_e_usd,
    preparation_e_usd,
    processing_e_usd,
)
from course_supporter.billing.kinds import ActionKind
from course_supporter.billing.quotes import Quote, author_quote, review_quote
from course_supporter.cost_estimate.actions import REQUIRED_FACTORS, MaterialFactors
from course_supporter.cost_estimate.node_summary import read_node_summary_factors
from course_supporter.homework.reference_key import AnswerKey
from course_supporter.homework.reference_service import ReadOnlyQueue
from course_supporter.homework.task_context import load_task_source_text
from course_supporter.homework.test_object import (
    DraftBody,
    published_form,
    render_for_prompt,
)
from course_supporter.homework.test_object_service import TestObjectService
from course_supporter.models.source import AssignmentType, SourceType
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from course_supporter.storage.test_object_repository import TestObjectRepository

router = APIRouter(tags=["billing"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]

_NOT_FOUND = "Not found."

UnpricedReason = Literal["missing_features", "estimate_failed", "nothing_to_do"]


class AuthorEstimateResponse(BaseModel):
    """The price of an author's action and whether it may start now."""

    price_units: int | None = Field(
        description="P: the price shown, in units; null when it cannot be "
        "priced (unpriced_reason says why)."
    )
    available_units: int | None = Field(
        description="What the paying account can spend now."
    )
    enough: bool = Field(
        description="May the action start now: available >= P, or no threshold "
        "applies (credit allowed), or charging is off. True when there is no "
        "price: the start checks the threshold on the material's real features."
    )
    missing_units: int | None = Field(
        description="P - available when not enough; null when there is no price."
    )
    charging_enabled: bool = Field(
        description="Charging applies to the payer of the action (every payer, "
        "or a marked one): while false nothing is refused."
    )
    unpriced_reason: UnpricedReason | None = Field(
        default=None,
        description="Why there is no price: missing_features — the body lacks a "
        "feature this kind of material needs (listed in missing_features); "
        "estimate_failed — the estimate itself failed. Or why the price is 0: "
        "nothing_to_do — the action would call no model (every node summary "
        "of the scope is fresh), nothing is reserved or charged. Null when "
        "priced.",
    )
    missing_features: list[str] | None = Field(
        default=None,
        description="The body's fields the kind needs and lacks, by their names "
        "in the request (duration_sec, slides, text_chars, files); null unless "
        "unpriced_reason is missing_features.",
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
        description="Charging applies to the review's payer (every payer, or a "
        "marked one) AND the task's path is not in its test mode."
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
    """What the client knows of a material before it is processed.

    The features a kind needs to be priced: audio and video — ``duration_sec``;
    presentation — ``slides`` and ``text_chars``; text and web —
    ``text_chars``; code — ``files`` and ``text_chars``
    (``cost_estimate.actions.REQUIRED_FACTORS``).
    """

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


def _author_response(
    quote: Quote, missing: list[str] | None = None, *, nothing_to_do: bool = False
) -> AuthorEstimateResponse:
    """The answer; without a price, why — the missing features, else a failure;
    at a price of 0 for want of work, ``nothing_to_do``."""
    reason: UnpricedReason | None = None
    if nothing_to_do:
        reason = "nothing_to_do"
    elif quote.price_units is None:
        reason = "missing_features" if missing else "estimate_failed"
    return AuthorEstimateResponse(
        price_units=quote.price_units,
        available_units=quote.available_units,
        enough=quote.enough,
        missing_units=quote.missing_units,
        charging_enabled=quote.charging_enabled,
        unpriced_reason=reason,
        missing_features=missing if reason == "missing_features" else None,
    )


def _missing_features(body: ProcessingEstimateRequest) -> list[str]:
    """The body's fields the kind of material needs and lacks, in its names."""
    return [name for name in REQUIRED_FACTORS[body.kind] if getattr(body, name) is None]


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
    read. A body without a feature the kind needs is not priced:
    ``unpriced_reason`` ``missing_features`` names the fields (BIL-E3).
    """
    root_id = await _course_root_of_node(session, tenant.tenant_id, node_id)
    missing = _missing_features(body)
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
        e_usd=None if missing else processing_e_usd(kind=body.kind, factors=factors),
    )
    return _author_response(quote, missing)


@router.post(
    "/nodes/{node_id}/summary/estimate",
    response_model=AuthorEstimateResponse,
)
async def estimate_node_summary_regeneration(
    tenant: PrepDep,
    session: SessionDep,
    node_id: uuid.UUID,
    body: NodeSummaryGenerateRequest,
) -> AuthorEstimateResponse:
    """The price of regenerating the section descriptions under this node.

    The author's processing (the Methodist's job,
    ``POST /nodes/{node_id}/summary/generate``): priced on the job's own
    factors — the scope and the freshness read by
    :func:`~course_supporter.cost_estimate.node_summary.read_node_summary_factors`
    — by the function the job reserves on
    (:func:`~course_supporter.billing.estimates.node_summary_e_usd`), so the
    price shown is the E reserved. The body is the generate route's; ``force``
    does not change the price (the run's scope is the node's subtree either
    way) and this route never answers its 422. Every summary fresh: price 0,
    ``unpriced_reason`` ``nothing_to_do``. Writes nothing.
    """
    del body  # the generate route's body; ``force`` is no factor of the price
    root_id = await _course_root_of_node(session, tenant.tenant_id, node_id)
    factors = await read_node_summary_factors(session, node_id)
    quote = await author_quote(
        session,
        action_kind=ActionKind.PROCESSING,
        tenant_id=tenant.tenant_id,
        course_node_id=root_id,
        e_usd=node_summary_e_usd(factors),
    )
    return _author_response(quote, nothing_to_do=not factors.calls_model)


@router.post(
    "/documents/{document_id}/reference/estimate",
    response_model=AuthorEstimateResponse,
)
async def estimate_key_explanation(
    tenant: PrepDep,
    session: SessionDep,
    document_id: uuid.UUID,
) -> AuthorEstimateResponse:
    """The price of the explanations of this test's key, asked by the author.

    Priced on what the worker will show the model for the version the author
    asks for now (:func:`_key_explanation_input`), by the worker's own rule
    (:func:`~course_supporter.billing.estimates.key_explanation_task_chars`):
    the price shown is the E the worker reserves.
    """
    task = await session.get(AuthoredDocument, document_id)
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    root_id = await _course_root_of_node(session, tenant.tenant_id, task.course_node_id)
    text, answers = await _key_explanation_input(session, task)
    quote = await author_quote(
        session,
        action_kind=ActionKind.TASK_PREPARATION,
        tenant_id=tenant.tenant_id,
        course_node_id=root_id,
        e_usd=preparation_e_usd(
            kind="key_explanation",
            task_chars=key_explanation_task_chars(text, answers),
        ),
    )
    return _author_response(quote)


async def _key_explanation_input(
    session: AsyncSession, task: AuthoredDocument
) -> tuple[str, AnswerKey]:
    """The text and the key the explanations asked for now would be written from.

    As the worker reads them (``workers/key_explain.py``, steps 2 and 4):

    * a test written in the system — its draft, lettered in the course's
      language. A check and a publication both ask by the axes of exactly that
      (``TestObjectService.check`` / ``publish``), and every body the worker
      finds by those axes has the same text and the same key.
    * any other test — its source text and the author's key in force; without
      a key yet, the text alone (an empty key).
    """
    if task.source_type == SourceType.TEST_OBJECT.value:
        draft = await TestObjectRepository(session).get_draft(task.id)
        if draft is None:
            msg = f"test {task.id} has no draft; a test is created with one"
            raise RuntimeError(msg)
        language = await TestObjectService(session, ReadOnlyQueue()).course_language(
            task.course_root_id
        )
        body = published_form(DraftBody.from_jsonb(draft.body), language)
        return render_for_prompt(body), body.answer_key()
    override = await TaskReferenceRepository(session).get_override(
        task.id, ReferenceKind.TEST_KEY
    )
    answers: AnswerKey = override.answers if override is not None else {}
    return await load_task_source_text(session, task.id), answers
