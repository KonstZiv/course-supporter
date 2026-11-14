"""Creem — a merchant of record — behind the payment port (BIL-PAY).

Everything Creem-specific lives here; billing sees the port's values only.
Read from Creem's documentation on 2026-11-14:

* API: https://docs.creem.io/api-reference/introduction — the test mode's
  host ``https://test-api.creem.io/v1`` and the live one
  ``https://api.creem.io/v1``, isolated, their keys not interchangeable; the
  key in the ``x-api-key`` header; amounts in cents (minor units),
  currencies as upper-case ISO codes.
* Checkout: ``POST /v1/checkouts`` —
  https://docs.creem.io/api-reference/endpoint/create-checkout. Sent:
  ``product_id`` (the catalog's ``providers.creem`` id of the package),
  ``units`` 1, ``success_url``, ``metadata`` {``account_id``,
  ``package_id``}; answered: ``id`` (``ch_…``) and ``checkout_url``. Creem
  takes no cancel URL and no locale, so neither is sent; no personal data is.
  ``metadata`` comes back on the checkout of ``checkout.completed`` (and on the
  ``checkout`` of a refund's or a dispute's event).
* Webhooks: https://docs.creem.io/code/webhooks — the ``creem-signature``
  header is HMAC-SHA256, hex, of the raw body with the webhook's secret; the
  body is ``{"id": "evt_…", "eventType": "…", "created_at": <ms>,
  "object": {…}}``. Creem retries a delivery that is not answered 2xx
  (5 attempts within 24 hours) and keeps its ``id``; a resend from the
  dashboard gets a new one.

Events:

=====================  ===================  ==================================
Creem ``eventType``    normalized event     from ``object``
=====================  ===================  ==================================
``checkout.completed`` PurchaseCompleted    ``metadata`` (account, package),
                                            ``order.amount_paid`` (tax
                                            included), ``order.currency``,
                                            ``order.id``; only an order
                                            ``paid`` (else Unknown)
``refund.created``     RefundApproved       ``id`` (``ref_…``: the adjustment),
                                            ``refund_amount``,
                                            ``refund_currency``, the order's
                                            id; sent once the refund
                                            ``succeeded``
``dispute.created``    Chargeback           ``id`` (``disp_…``), ``amount``,
                                            ``currency``, the order's id,
                                            ``checkout.metadata`` (account)
anything else          Unknown              —
=====================  ===================  ==================================

Ids: a Creem order is one product bought once — ``order.id`` (``ord_…``) is
both the transaction and the line of a purchase, as an invoice's number is.
It is required on every order (``order.transaction``, the payment's
``tran_…``, may be null), and a refund's and a dispute's event name it
(``object.order.id``, ``object.transaction.order``), so they find their batch.

Refunds: Creem's API has no endpoint that makes a refund — neither the API
reference nor the TypeScript SDK (https://docs.creem.io/code/sdks/typescript)
lists one; refunds are made in the Creem dashboard. :meth:`request_refund`
raises :class:`~.port.RefundUnavailableError`, and the journal follows such a
refund by its ``refund.created``
(:func:`~course_supporter.billing.refunds.refund_at_provider`).

Disputes: ``dispute.created`` holds the purchase's free units. Creem sends no
event when a dispute is won (https://docs.creem.io/integrations/refgrow), nor
one when it is lost, so the administrator ends it
(``scripts/billing_admin.py dispute-lost`` / ``dispute-won``).

HTTP: a client per call, with a timeout, and no retries — as everywhere in the
project (the SDKs' own retries are off): the payer asks again. A network
error, a timeout, a 5xx or 429 is :class:`~.port.PaymentProviderError` (502);
a 404 for the product is :class:`~.port.CheckoutUnavailableError` (503). The
API key and the webhook secret are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from course_supporter.billing.payments.port import (
    Chargeback,
    Checkout,
    CheckoutRequest,
    CheckoutUnavailableError,
    InvalidWebhookError,
    PaymentProviderError,
    ProviderEvent,
    PurchaseCompleted,
    RefundApproved,
    RefundRequest,
    RefundUnavailableError,
    Unknown,
)

log = structlog.get_logger()

CREEM: Final = "creem"
"""The adapter's name (``BILLING_PROVIDERS=["creem"]``)."""

TEST_API_BASE: Final = "https://test-api.creem.io/v1"
LIVE_API_BASE: Final = "https://api.creem.io/v1"

SIGNATURE_HEADER: Final = "creem-signature"

CHECKOUT_COMPLETED: Final = "checkout.completed"
REFUND_CREATED: Final = "refund.created"
DISPUTE_CREATED: Final = "dispute.created"

TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
"""Of one call to Creem's API; a checkout is opened while the payer waits."""

_PAID: Final = "paid"
_SUCCEEDED: Final = "succeeded"


# ── Creem's shapes (only what billing reads; the rest is ignored) ───────


class _Envelope(BaseModel):
    id: str
    eventType: str  # noqa: N815 — Creem's own name
    created_at: int | None = None
    """Milliseconds since the epoch."""
    object: dict[str, Any]


class _Order(BaseModel):
    id: str
    amount_paid: int | None = None
    currency: str
    status: str | None = None


class _CheckoutRef(BaseModel):
    metadata: dict[str, Any] | None = None


class _CheckoutCompleted(BaseModel):
    id: str
    order: _Order
    units: int | None = None
    metadata: dict[str, Any] | None = None


class _OrderRef(BaseModel):
    id: str


class _TransactionRef(BaseModel):
    id: str | None = None
    order: str | None = None


class _Refund(BaseModel):
    id: str
    status: str | None = None
    refund_amount: int
    refund_currency: str
    transaction: _TransactionRef | str | None = None
    order: _OrderRef | str | None = None


class _Dispute(BaseModel):
    id: str
    amount: int
    currency: str
    transaction: _TransactionRef | str | None = None
    order: _OrderRef | str | None = None
    checkout: _CheckoutRef | str | None = None


class _CheckoutCreated(BaseModel):
    id: str
    checkout_url: str


def _order_id(
    order: _OrderRef | str | None, transaction: _TransactionRef | str | None
) -> str | None:
    """The order's id: from the order, else from the transaction it paid."""
    if isinstance(order, _OrderRef):
        return order.id
    if isinstance(order, str):
        return order
    if isinstance(transaction, _TransactionRef):
        return transaction.order
    return None


def _metadata(value: Mapping[str, Any] | None, key: str) -> str | None:
    found = (value or {}).get(key)
    return found if isinstance(found, str) and found else None


def _account(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _instant(milliseconds: int | None) -> datetime | None:
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


# ── The adapter ────────────────────────────────────────────────────────


class CreemProvider:
    """Creem's API and webhooks behind the port (module docstring)."""

    name: Final = CREEM

    def __init__(
        self,
        *,
        api_key: str,
        webhook_secret: str,
        api_base: str = TEST_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``transport`` replaces the network (tests)."""
        if not api_key or not webhook_secret:
            msg = "creem needs an API key and a webhook secret"
            raise ValueError(msg)
        self._api_key = api_key
        self._secret = webhook_secret.encode()
        self._api_base = api_base
        self._transport = transport

    def __repr__(self) -> str:
        return f"CreemProvider(api_base={self._api_base!r})"

    def sign(self, body: bytes) -> str:
        """The ``creem-signature`` of this body."""
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    # ── The port ──

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        """``POST /checkouts`` of the package's Creem product (module docstring)."""
        product = request.provider_item
        if product is None:
            raise CheckoutUnavailableError(
                f"creem has no product for package {request.package.id}"
            )
        payload = {
            "product_id": product,
            "units": 1,
            "success_url": request.success_url,
            "metadata": {
                "account_id": str(request.account_id),
                "package_id": request.package.id,
            },
        }
        response = await self._post("/checkouts", payload)
        if response.status_code == httpx.codes.NOT_FOUND:
            log.error(
                "billing_creem_product_unknown",
                package_id=request.package.id,
                product_id=product,
                api_base=self._api_base,
            )
            raise CheckoutUnavailableError(f"creem knows no product {product}")
        body = self._answer(response, "/checkouts")
        try:
            created = _CheckoutCreated.model_validate(body)
        except ValidationError as exc:
            msg = "creem answered a checkout without its id or link"
            raise PaymentProviderError(msg) from exc
        return Checkout(url=created.checkout_url, checkout_id=created.id)

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ProviderEvent]:
        """Check ``creem-signature``, then read the one event of the body."""
        signature = headers.get(SIGNATURE_HEADER)
        if not signature:
            raise InvalidWebhookError("no signature")
        if not hmac.compare_digest(
            signature.strip().encode(), self.sign(body).encode()
        ):
            raise InvalidWebhookError("a wrong signature")
        try:
            envelope = _Envelope.model_validate(json.loads(body))
            return [self._event(envelope)]
        except ValueError as exc:  # not JSON, or JSON of another shape
            # Not the error's text: it may quote the payer's data.
            raise InvalidWebhookError("not an event of creem") from exc

    async def request_refund(self, request: RefundRequest) -> str:
        """Creem's API makes no refunds (module docstring)."""
        raise RefundUnavailableError(
            f"creem refunds in its dashboard only (transaction "
            f"{request.transaction_id}); its refund.created tells the journal"
        )

    # ── HTTP ──

    async def _post(self, path: str, payload: Mapping[str, Any]) -> httpx.Response:
        """One call; a network error or a timeout is PaymentProviderError."""
        try:
            async with httpx.AsyncClient(
                base_url=self._api_base,
                headers={"x-api-key": self._api_key, "accept": "application/json"},
                timeout=TIMEOUT,
                transport=self._transport,
            ) as client:
                return await client.post(path, json=payload)
        except httpx.HTTPError as exc:
            log.warning(
                "billing_creem_unreachable", path=path, error=type(exc).__name__
            )
            msg = f"creem {path}: {type(exc).__name__}"
            raise PaymentProviderError(msg) from exc

    def _answer(self, response: httpx.Response, path: str) -> Any:
        """The JSON of a 2xx answer; anything else is PaymentProviderError."""
        if not response.is_success:
            trace = None
            try:
                found = response.json()
                trace = found.get("trace_id") if isinstance(found, dict) else None
            except ValueError:
                pass
            log.warning(
                "billing_creem_refused",
                path=path,
                status=response.status_code,
                trace_id=trace,
            )
            msg = f"creem {path} answered {response.status_code}"
            raise PaymentProviderError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"creem {path} answered no JSON"
            raise PaymentProviderError(msg) from exc

    # ── Reading an event ──

    def _event(self, envelope: _Envelope) -> ProviderEvent:
        common: dict[str, Any] = {
            "event_id": envelope.id,
            "provider_type": envelope.eventType,
            "occurred_at": _instant(envelope.created_at),
        }
        if envelope.eventType == CHECKOUT_COMPLETED:
            return self._purchase(envelope.object, common)
        if envelope.eventType == REFUND_CREATED:
            return self._refund(envelope.object, common)
        if envelope.eventType == DISPUTE_CREATED:
            return self._dispute(envelope.object, common)
        return Unknown(**common)

    @staticmethod
    def _purchase(data: dict[str, Any], common: dict[str, Any]) -> ProviderEvent:
        paid = _CheckoutCompleted.model_validate(data)
        if paid.order.status != _PAID:
            return Unknown(**common)
        if paid.units not in (None, 1):
            msg = f"a checkout of {paid.units} units: one package is one unit"
            raise InvalidWebhookError(msg)
        package_id = _metadata(paid.metadata, "package_id")
        if package_id is None:
            raise InvalidWebhookError("a purchase names no package")
        if paid.order.amount_paid is None:
            raise InvalidWebhookError("a purchase without the amount paid")
        return PurchaseCompleted(
            **common,
            account_id=_account(_metadata(paid.metadata, "account_id")),
            package_id=package_id,
            amount_paid_minor=paid.order.amount_paid,
            currency=paid.order.currency,
            transaction_id=paid.order.id,
            line_id=paid.order.id,
        )

    @staticmethod
    def _refund(data: dict[str, Any], common: dict[str, Any]) -> ProviderEvent:
        refund = _Refund.model_validate(data)
        if refund.status not in (None, _SUCCEEDED):
            return Unknown(**common)
        return RefundApproved(
            **common,
            adjustment_id=refund.id,
            transaction_id=_order_id(refund.order, refund.transaction),
            amount_minor=refund.refund_amount,
            currency=refund.refund_currency,
        )

    @staticmethod
    def _dispute(data: dict[str, Any], common: dict[str, Any]) -> ProviderEvent:
        dispute = _Dispute.model_validate(data)
        order = _order_id(dispute.order, dispute.transaction)
        if order is None:
            raise InvalidWebhookError("a dispute names no order")
        metadata = (
            dispute.checkout.metadata
            if isinstance(dispute.checkout, _CheckoutRef)
            else None
        )
        return Chargeback(
            **common,
            dispute_id=dispute.id,
            account_id=_account(_metadata(metadata, "account_id")),
            transaction_id=order,
            amount_minor=dispute.amount,
            currency=dispute.currency,
        )
