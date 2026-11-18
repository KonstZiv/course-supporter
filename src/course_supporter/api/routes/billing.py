"""Payments outside the portal (BIL-PAY): providers' webhooks, the author's checkout.

Routes
------
- ``POST /billing/webhooks/{provider}`` — a payment provider tells what
  happened. No API key: the adapter checks the provider's signature, and a
  request without a valid one is 400 with nothing recorded. A provider that
  is not switched on is 404 — as is any other name. The body is read up to
  :data:`MAX_WEBHOOK_BYTES` (413 beyond). Each event is handled in a
  transaction of its own (``billing.payments.webhooks``): 200 when every
  event was handled or was a repeat, 500 when one failed — it is recorded,
  and the provider sends it again. No answer says more than its status.
- ``GET /billing/terms`` — the revision of the billing terms in force and
  whether the author (the tenant's key, scope ``prep``) still has to agree to
  it. Creates nothing.
- ``POST /billing/consent`` — the author agrees to the revision they were
  shown, once per revision, as a student does in the portal (operator
  2026-11-12): 201 with the new record, 200 with the earlier one, 409 when the
  revision is not the one in force. Channel ``api``.
- ``GET /billing/packages`` — the packages on sale and whether the author
  may buy now (``purchase``), as the portal's (the same function). Creates no
  account.
- ``POST /billing/checkout`` — an author (the tenant's key, scope ``prep``)
  opens a checkout of a package for the tenant's author account, which pays
  for processing and, under a payment rule, for the reviews of the tenant's
  students. The same service as the portal's checkout; 503 when payment is
  unavailable, 409 while the author has not agreed to the terms in force, 403
  while the provider is in test mode and the account is not marked for test
  purchases, 404 for a package not on sale or a provider not switched on.
  The answer carries the checkout's own number (``checkout_ref``), which is
  also the last segment of the address the author comes back to:
  ``{AUTHOR_APP_BASE_URL}/billing/checkouts/{checkout_ref}``.
- ``GET /billing/checkouts/{checkout_ref}`` — the state of the author's
  checkout (pending | credited | failed); another's or an unknown one is 404
  ``checkout_not_found``.
- ``GET /billing/balance`` — where the author's units stand: balance,
  reserved, held, available, difference, the next expiry. Units only.
- ``GET /billing/statement?limit=&cursor=&from=&to=`` — the author's
  movements of units, the newest first, a page at a time (``invalid_cursor``
  422 for a cursor this server did not give).
- ``GET /billing/spending?from=YYYY-MM-DD&to=YYYY-MM-DD`` — what the author's
  actions cost them in units, by course and homework task
  (:mod:`course_supporter.billing.spending`); the current UTC month by
  default, both days included; ``invalid_period`` 422 when ``from`` is after
  ``to``. Never a dollar.

Every refusal of a route but the webhook carries its reason code
(``_billing_shared``).

There is no administrator route (operator decision of 2026-11-05): the
administrator's door is ``scripts/billing_admin.py``.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_billing_catalog,
    get_payment_registry,
    get_session,
)
from course_supporter.api.routes._billing_shared import (
    CHECKOUT_REFUSALS,
    Refusal,
    balance_of,
    checkout_state_of,
    consent_required,
    open_checkout,
    packages_of,
    refusal,
    require_payment,
    responses,
    statement_of,
    terms_changed,
)
from course_supporter.api.schemas import (
    BillingBalanceResponse,
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingCheckoutStateResponse,
    BillingConsentRequest,
    BillingConsentResponse,
    BillingPackagesResponse,
    BillingSpendingCourse,
    BillingSpendingPeriod,
    BillingSpendingResponse,
    BillingSpendingTask,
    BillingSpendingUnits,
    BillingStatementResponse,
    BillingTermsResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.billing import charging
from course_supporter.billing.accounts import (
    find_active_account,
    get_or_create_author_account,
)
from course_supporter.billing.consent import (
    author_terms_status,
    record_author_consent,
)
from course_supporter.billing.costs import UnitsSummary, default_month_bounds
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import AccountKind, ActionKind
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.checkout import is_test_purchase
from course_supporter.billing.payments.port import InvalidWebhookError
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.payments.webhooks import (
    EventHandlingError,
    handle_events,
)
from course_supporter.billing.spending import Spending, author_spending
from course_supporter.billing.statement import MAX_LIMIT
from course_supporter.config import get_settings

router = APIRouter(tags=["billing"])
logger = structlog.get_logger()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_payment_registry)]
CatalogDep = Annotated[Catalog, Depends(get_billing_catalog)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

MAX_WEBHOOK_BYTES: Final = 256 * 1024
"""The largest webhook body read; a provider's event is a few kilobytes."""


def _status(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _read_body(request: Request) -> bytes:
    """The raw body, refused past :data:`MAX_WEBHOOK_BYTES` (413)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise _status(413, "Payload Too Large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_WEBHOOK_BYTES:
            raise _status(413, "Payload Too Large")
    return bytes(body)


@router.post(
    "/billing/webhooks/{provider}",
    responses={
        200: {"description": "Every event handled, or a repeat."},
        400: {"description": "No valid signature, or not the provider's event."},
        404: {"description": "No such provider switched on."},
        413: {"description": "The body is too large."},
        500: {"description": "An event failed; send it again."},
    },
)
async def receive_webhook(
    provider: str,
    request: Request,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> Response:
    """A payment provider's webhook (module docstring)."""
    adapter = registry.get(provider)
    if adapter is None:
        raise _status(404, "Not Found")
    body = await _read_body(request)
    try:
        events = adapter.parse_webhook(request.headers, body)
    except InvalidWebhookError as exc:
        logger.warning("billing_webhook_refused", provider=provider, reason=str(exc))
        raise _status(400, "Bad Request") from exc
    try:
        await handle_events(
            session,
            provider=adapter.name,
            events=events,
            body_sha256=hashlib.sha256(body).hexdigest(),
            catalog=catalog,
            test_purchases=is_test_purchase(
                adapter, marked_only=registry.purchase_marked_only
            ),
        )
    except EventHandlingError:
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )
    return Response(status_code=200)


@router.get("/billing/terms")
async def get_author_billing_terms(
    tenant: PrepDep, session: SessionDep
) -> BillingTermsResponse:
    """The billing terms in force and whether the author has agreed to them."""
    status = await author_terms_status(
        session,
        tenant_id=tenant.tenant_id,
        terms_version=get_settings().billing_terms_version,
    )
    return BillingTermsResponse(
        terms_version=status.terms_version,
        consent_required=status.consent_required,
        consented_at=status.consented_at,
    )


@router.post(
    "/billing/consent",
    status_code=201,
    responses={
        200: {"description": "Already agreed to this revision; the earlier record."},
        **responses(Refusal.TERMS_CHANGED),
    },
)
async def post_author_billing_consent(
    body: BillingConsentRequest,
    tenant: PrepDep,
    session: SessionDep,
    response: Response,
) -> BillingConsentResponse:
    """Record the author's consent to the revision in force."""
    current = get_settings().billing_terms_version
    try:
        record, created = await record_author_consent(
            session,
            tenant_id=tenant.tenant_id,
            terms_version=body.terms_version,
            current_version=current,
            text_sha256=body.text_sha256,
        )
    except BillingConflictError as exc:
        raise terms_changed(current) from exc
    await session.commit()
    if created:
        logger.info(
            "billing_consent_recorded",
            tenant_id=str(tenant.tenant_id),
            account_kind="author",
            terms_version=record.terms_version,
        )
    else:
        response.status_code = 200
    return BillingConsentResponse(
        terms_version=record.terms_version, consented_at=record.consented_at
    )


@router.get("/billing/packages")
async def get_author_billing_packages(
    tenant: PrepDep,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> BillingPackagesResponse:
    """The packages on sale, and whether the author may buy them now."""
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant.tenant_id
    )
    return packages_of(catalog, registry, account)


@router.post("/billing/checkout", responses=responses(*CHECKOUT_REFUSALS))
async def post_author_checkout(
    body: BillingCheckoutRequest,
    tenant: PrepDep,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> BillingCheckoutResponse:
    """Open a checkout of a package; the units go to the tenant's author account."""
    require_payment(registry)
    current = get_settings().billing_terms_version
    terms = await author_terms_status(
        session, tenant_id=tenant.tenant_id, terms_version=current
    )
    if terms.consent_required:
        raise consent_required(current)
    account = await get_or_create_author_account(session, tenant_id=tenant.tenant_id)
    await session.commit()  # no transaction stays open across the provider's call
    author_app = get_settings().author_app_base_url.rstrip("/")
    return await open_checkout(
        body,
        session=session,
        registry=registry,
        catalog=catalog,
        account=account,
        language=None,
        return_page=f"{author_app}/billing",
    )


@router.get(
    "/billing/checkouts/{checkout_ref}",
    responses=responses(Refusal.CHECKOUT_NOT_FOUND),
)
async def get_author_checkout(
    checkout_ref: str, tenant: PrepDep, session: SessionDep
) -> BillingCheckoutStateResponse:
    """The state of the author's checkout by its number."""
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant.tenant_id
    )
    return await checkout_state_of(session, account, checkout_ref)


@router.get("/billing/balance")
async def get_author_balance(
    tenant: PrepDep, session: SessionDep
) -> BillingBalanceResponse:
    """Where the author's units stand."""
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant.tenant_id
    )
    return await balance_of(session, account)


@router.get("/billing/statement", responses=responses(Refusal.INVALID_CURSOR))
async def get_author_statement(
    tenant: PrepDep,
    session: SessionDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_LIMIT, description="Lines on the page.")
    ] = 50,
    cursor: Annotated[
        str | None,
        Query(max_length=256, description="next_cursor of the page before."),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(alias="from", description="From (inclusive); without a zone, UTC."),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(alias="to", description="To (exclusive); without a zone, UTC."),
    ] = None,
) -> BillingStatementResponse:
    """The author's movements of units, the newest first."""
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant.tenant_id
    )
    return await statement_of(
        session, account, limit=limit, cursor=cursor, since=since, until=until
    )


def _units(units: UnitsSummary) -> BillingSpendingUnits:
    return BillingSpendingUnits(
        billed_units=units.billed_units, covered_units=units.covered_units
    )


@router.get("/billing/spending", responses=responses(Refusal.INVALID_PERIOD))
async def get_author_spending(
    tenant: PrepDep,
    session: SessionDep,
    from_date: Annotated[
        date | None,
        Query(alias="from", description="The first day, UTC (default: the 1st)."),
    ] = None,
    to_date: Annotated[
        date | None,
        Query(alias="to", description="The last day, UTC, included (default: today)."),
    ] = None,
) -> BillingSpendingResponse:
    """What the author's actions cost them, in units, by course and task."""
    default_from, default_to = default_month_bounds()
    start, end = from_date or default_from, to_date or default_to
    if start > end:
        raise refusal(Refusal.INVALID_PERIOD, "The period starts after it ends.")
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant.tenant_id
    )
    found = (
        await author_spending(
            session, account_id=account.id, from_date=start, to_date=end
        )
        if account is not None
        else Spending(start, end, UnitsSummary(), [], UnitsSummary())
    )
    return BillingSpendingResponse(
        period=BillingSpendingPeriod(from_date=start, to_date=end),
        charging_enabled=await charging.charging_applies(
            session, account.id if account is not None else None
        ),
        totals=_units(found.totals),
        courses=[
            BillingSpendingCourse(
                course_node_id=course.course_node_id,
                title=course.title,
                billed_units=course.units.billed_units,
                covered_units=course.units.covered_units,
                reviews_units=course.by_kind[ActionKind.REVIEW],
                processing_units=course.by_kind[ActionKind.PROCESSING],
                preparation_units=course.by_kind[ActionKind.TASK_PREPARATION],
                tasks=[
                    BillingSpendingTask(
                        authored_document_id=task.authored_document_id,
                        title=task.title,
                        reviews=task.reviews,
                        billed_units=task.units.billed_units,
                        covered_units=task.units.covered_units,
                    )
                    for task in course.tasks
                ],
            )
            for course in found.courses
        ],
        unlinked=_units(found.unlinked),
    )
