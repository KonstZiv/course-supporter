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
- ``POST /billing/checkout`` — an author (the tenant's key, scope ``prep``)
  opens a checkout of a package for the tenant's author account, which pays
  for processing and, under a payment rule, for the reviews of the tenant's
  students. The same service as the portal's checkout; 503 when payment is
  unavailable, 404 for a package not on sale or a provider not switched on.

There is no administrator route (operator decision of 2026-11-05): the
administrator's door is ``scripts/billing_admin.py``.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_billing_catalog,
    get_payment_registry,
    get_session,
)
from course_supporter.api.routes._billing_shared import (
    CHECKOUT_RESPONSES,
    open_checkout,
    require_payment,
)
from course_supporter.api.schemas import BillingCheckoutRequest, BillingCheckoutResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.port import InvalidWebhookError
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.payments.webhooks import (
    EventHandlingError,
    handle_events,
)
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
        )
    except EventHandlingError:
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )
    return Response(status_code=200)


@router.post("/billing/checkout", responses=CHECKOUT_RESPONSES)
async def post_author_checkout(
    body: BillingCheckoutRequest,
    tenant: PrepDep,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> BillingCheckoutResponse:
    """Open a checkout of a package; the units go to the tenant's author account."""
    require_payment(registry)
    account = await get_or_create_author_account(session, tenant_id=tenant.tenant_id)
    await session.commit()  # no transaction stays open across the provider's call
    author_app = get_settings().author_app_base_url.rstrip("/")
    return await open_checkout(
        body,
        registry=registry,
        catalog=catalog,
        account_id=account.id,
        language=None,
        return_page=f"{author_app}/billing",
    )
