"""Macropay — a merchant of record, the second one — behind the payment port (BIL-PAY).

The fallback way to sell if Creem refuses or delays its review. Switched on by
``BILLING_PROVIDERS`` only (production: ``["creem"]``, Macropay off).
Everything Macropay-specific lives here; billing sees the port's values only.
Written from Macropay's documentation as read on 2026-11-15 (docs.macropay.ai:
integrate/sandbox, integrate/authentication, integrate/webhooks/*,
features/checkout/session, api-reference/checkouts/create-session,
api-reference/orders/get, features/refunds, api-reference/refunds/create)
WITHOUT a sandbox: what those pages do not say outright is listed as not
verified in ``billing/README.md``, for the live sandbox check to close.

* API: the sandbox ``https://sandbox-api.macropay.ai/v1`` and the live
  ``https://api.macropay.ai/v1``; an Organization Access Token in
  ``Authorization: Bearer``, one prefix for both, not interchangeable.
  Events carry no test flag, so :attr:`MacropayProvider.test_mode` is False
  on the live host only: any other is the sandbox, so a mistaken address
  closes purchases rather than opens them.
* Checkout: ``POST /v1/checkouts/``. Sent: ``products`` [the catalog's
  ``providers.macropay`` id of the package], ``currency`` ``eur``,
  ``success_url`` (Macropay appends nothing; the platform's number of the
  checkout travels in its path), ``return_url`` (the payment page's "back"),
  ``metadata`` {``account_id``, ``package_id``, ``checkout_ref``} — copied to
  the order and returned in ``data.metadata`` of ``order.*``. No personal
  data, no customer id. Answered: ``id`` and ``url``.
* Webhooks: Standard Webhooks
  (https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md).
  ``webhook-signature`` is ``v1,<base64>`` (several, space-separated, while
  a secret rotates) — HMAC-SHA256 of ``{webhook-id}.{webhook-timestamp}.{raw
  body}`` with the secret ``whsec_<base64>`` DECODED; ``webhook-timestamp``
  (seconds) within 5 minutes of now; compared in constant time. The body is
  the spec's ``{"type", "timestamp", "data"}``; it has no id of its own, so
  ``webhook-id`` — kept across Macropay's retries (10 attempts) — is the
  event's id. Whether a resend from the dashboard keeps it is not verified:
  the journal is idempotent by ``order.id`` and ``refund.id`` too.

Events:

==============================  ===================  ===========================
Macropay ``type``               normalized event     from ``data``
==============================  ===================  ===========================
``order.paid``                  PurchaseCompleted    ``metadata`` (account,
                                                     package, checkout),
                                                     ``total_amount`` (tax
                                                     included), ``currency``,
                                                     ``id``; only an order
                                                     ``paid`` (else Unknown)
``refund.created`` /            RefundApproved       ``id`` (the adjustment),
``refund.updated``                                   ``order_id``, ``amount`` +
                                                     ``tax_amount``,
                                                     ``currency``; only a refund
                                                     ``succeeded`` (else
                                                     Unknown)
anything else                   Unknown              —
==============================  ===================  ===========================

Ids: an order is one product bought once — ``order.id`` is both the
transaction and the line of a purchase, as for Creem; a refund names it
(``order_id``), so it finds its batch.

Refunds: partial ones too. A refund ``succeeded`` (``refund.created`` or
``refund.updated``, whichever says so first; the other is a repeat of the
same adjustment) takes the units — completing the administrator's hold of the
order, if any (:func:`~course_supporter.billing.refunds.refund_at_provider`).
``order.refunded`` repeats the order's running total under no refund id, so
it is Unknown: as a second approval it would take the units twice. A refund
``failed`` or ``canceled`` is Unknown: no hold is keyed by a refund id
(``refund-release`` frees the order's hold, as for Creem). The amount is
``amount`` + ``tax_amount``: the purchase's figure, ``total_amount``,
includes the tax. A refund with ``reason`` ``dispute_prevention`` is a
refund like any other.

Disputes: Macropay handles them and its catalog has no dispute event, so no
:class:`~.port.Chargeback` is made; an event of a type not listed above is
recorded as ignored under its own type (``billing_provider_events``).

A failed payment (``checkout.updated`` ``failed`` / ``expired``) is Unknown:
the checkout stays unpaid, as with Creem.

HTTP: a client per call, with a timeout, and no retries, as for Creem. A
network error, a timeout or an answer other than 2xx is
:class:`~.port.PaymentProviderError` (502). The token and the webhook secret
are never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from course_supporter.billing.payments.port import (
    Checkout,
    CheckoutRequest,
    CheckoutUnavailableError,
    InvalidWebhookError,
    PaymentProviderError,
    ProviderEvent,
    PurchaseCompleted,
    RefundApproved,
    RefundRequest,
    Unknown,
)

log = structlog.get_logger()

MACROPAY: Final = "macropay"
"""The adapter's name (``BILLING_PROVIDERS=["macropay"]``)."""

SANDBOX_API_BASE: Final = "https://sandbox-api.macropay.ai/v1"
LIVE_API_BASE: Final = "https://api.macropay.ai/v1"

ID_HEADER: Final = "webhook-id"
TIMESTAMP_HEADER: Final = "webhook-timestamp"
SIGNATURE_HEADER: Final = "webhook-signature"
SECRET_PREFIX: Final = "whsec_"  # noqa: S105 — the format's prefix, not a secret
TOLERANCE_SECONDS: Final = 5 * 60
"""How far ``webhook-timestamp`` may be from now, either way."""

ORDER_PAID: Final = "order.paid"
REFUND_EVENTS: Final = frozenset({"refund.created", "refund.updated"})

CURRENCY: Final = "eur"
"""Of every checkout: each product has one fixed EUR price."""
REFUND_REASON: Final = "customer_request"

TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
"""Of one call to Macropay's API; a checkout is opened while the payer waits."""

_PAID: Final = "paid"
_SUCCEEDED: Final = "succeeded"


# ── Macropay's shapes (only what billing reads; the rest is ignored) ────


class _Envelope(BaseModel):
    type: str
    timestamp: datetime | None = None
    data: dict[str, Any]


class _Order(BaseModel):
    id: str
    status: str | None = None
    total_amount: int | None = None
    currency: str
    metadata: dict[str, Any] | None = None


class _Refund(BaseModel):
    id: str
    order_id: str
    amount: int
    tax_amount: int | None = None
    currency: str
    status: str | None = None


class _Created(BaseModel):
    id: str


class _CheckoutCreated(_Created):
    url: str


def _metadata(value: Mapping[str, Any] | None, key: str) -> str | None:
    found = (value or {}).get(key)
    return found if isinstance(found, str) and found else None


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _secret(value: str) -> bytes:
    """The key of ``whsec_<base64>``: its base64 decoded (the prefix optional).

    Raises:
        ValueError: not base64, or nothing in it.
    """
    encoded = value.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        msg = "macropay's webhook secret is whsec_ and base64"
        raise ValueError(msg) from exc
    if not key:
        msg = "macropay's webhook secret is empty"
        raise ValueError(msg)
    return key


# ── The adapter ────────────────────────────────────────────────────────


class MacropayProvider:
    """Macropay's API and webhooks behind the port (module docstring)."""

    name: Final = MACROPAY

    def __init__(
        self,
        *,
        api_key: str,
        webhook_secret: str,
        api_base: str = SANDBOX_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``transport`` replaces the network, ``clock`` the time (tests).

        Raises:
            ValueError: no token, or a secret that is not ``whsec_<base64>``.
        """
        if not api_key:
            msg = "macropay needs an access token"
            raise ValueError(msg)
        self._api_key = api_key
        self._secret = _secret(webhook_secret)
        self._api_base = api_base
        self._transport = transport
        self._clock = clock

    def __repr__(self) -> str:
        return f"MacropayProvider(api_base={self._api_base!r})"

    @property
    def test_mode(self) -> bool:
        """False on Macropay's live host only (module docstring)."""
        return urlsplit(self._api_base).hostname != urlsplit(LIVE_API_BASE).hostname

    def sign(self, webhook_id: str, timestamp: int, body: bytes) -> str:
        """The ``webhook-signature`` (``v1,<base64>``) of this delivery."""
        signed = f"{webhook_id}.{timestamp}.".encode() + body
        digest = hmac.new(self._secret, signed, hashlib.sha256).digest()
        return f"v1,{base64.b64encode(digest).decode()}"

    # ── The port ──

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        """``POST /checkouts/`` of the package's Macropay product (module docstring)."""
        product = request.provider_item
        if product is None:
            raise CheckoutUnavailableError(
                f"macropay has no product for package {request.package.id}"
            )
        payload = {
            "products": [product],
            "currency": CURRENCY,
            "success_url": request.success_url,
            "return_url": request.cancel_url,
            "metadata": {
                "account_id": str(request.account_id),
                "package_id": request.package.id,
                "checkout_ref": str(request.checkout_ref),
            },
        }
        body = await self._call("/checkouts/", payload)
        try:
            created = _CheckoutCreated.model_validate(body)
        except ValidationError as exc:
            msg = "macropay answered a checkout without its id or link"
            raise PaymentProviderError(msg) from exc
        return Checkout(url=created.url, checkout_id=created.id)

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ProviderEvent]:
        """Check the Standard Webhooks signature, then read the one event."""
        webhook_id = self._verify(headers, body)
        try:
            envelope = _Envelope.model_validate(json.loads(body))
            return [self._event(webhook_id, envelope)]
        except ValueError as exc:  # not JSON, or JSON of another shape
            # Not the error's text: it may quote the payer's data.
            raise InvalidWebhookError("not an event of macropay") from exc

    async def request_refund(self, request: RefundRequest) -> str:
        """``POST /refunds/`` of the journal's amount; Macropay's refund id.

        Partial refunds are allowed. Nothing calls this yet: refunds are made
        in the dashboard after ``refund-hold`` (``scripts/billing_admin.py``).
        """
        payload = {
            "order_id": request.transaction_id,
            "amount": request.amount_minor,
            "reason": REFUND_REASON,
            "metadata": {"batch_id": str(request.batch_id)},
        }
        body = await self._call("/refunds/", payload)
        try:
            return _Created.model_validate(body).id
        except ValidationError as exc:
            msg = "macropay answered a refund without its id"
            raise PaymentProviderError(msg) from exc

    # ── The signature ──

    def _verify(self, headers: Mapping[str, str], body: bytes) -> str:
        """The delivery's ``webhook-id`` once its signature holds."""
        webhook_id = headers.get(ID_HEADER)
        sent_at = headers.get(TIMESTAMP_HEADER)
        signatures = headers.get(SIGNATURE_HEADER)
        if not (webhook_id and sent_at and signatures):
            raise InvalidWebhookError("no signature")
        try:
            timestamp = int(sent_at)
        except ValueError as exc:
            raise InvalidWebhookError("a timestamp that is not one") from exc
        if abs(self._clock() - timestamp) > TOLERANCE_SECONDS:
            raise InvalidWebhookError("a timestamp outside the 5 minutes")
        expected = self.sign(webhook_id, timestamp, body).encode()
        if not any(
            hmac.compare_digest(expected, sent.encode()) for sent in signatures.split()
        ):
            raise InvalidWebhookError("a wrong signature")
        return webhook_id

    # ── HTTP ──

    async def _call(self, path: str, payload: Mapping[str, Any]) -> Any:
        """One POST; the JSON of a 2xx answer, else PaymentProviderError."""
        try:
            async with httpx.AsyncClient(
                base_url=self._api_base,
                headers={
                    "authorization": f"Bearer {self._api_key}",
                    "accept": "application/json",
                },
                timeout=TIMEOUT,
                transport=self._transport,
            ) as client:
                response = await client.post(path, json=payload)
        except httpx.HTTPError as exc:
            log.warning(
                "billing_macropay_unreachable", path=path, error=type(exc).__name__
            )
            msg = f"macropay {path}: {type(exc).__name__}"
            raise PaymentProviderError(msg) from exc
        if not response.is_success:
            log.warning(
                "billing_macropay_refused", path=path, status=response.status_code
            )
            msg = f"macropay {path} answered {response.status_code}"
            raise PaymentProviderError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"macropay {path} answered no JSON"
            raise PaymentProviderError(msg) from exc

    # ── Reading an event ──

    def _event(self, webhook_id: str, envelope: _Envelope) -> ProviderEvent:
        common: dict[str, Any] = {
            "event_id": webhook_id,
            "provider_type": envelope.type,
            "occurred_at": envelope.timestamp,
        }
        if envelope.type == ORDER_PAID:
            return self._purchase(envelope.data, common)
        if envelope.type in REFUND_EVENTS:
            return self._refund(envelope.data, common)
        return Unknown(**common)

    @staticmethod
    def _purchase(data: dict[str, Any], common: dict[str, Any]) -> ProviderEvent:
        order = _Order.model_validate(data)
        if order.status != _PAID:
            return Unknown(**common)
        package_id = _metadata(order.metadata, "package_id")
        if package_id is None:
            raise InvalidWebhookError("a purchase names no package")
        if order.total_amount is None:
            raise InvalidWebhookError("a purchase without the amount paid")
        return PurchaseCompleted(
            **common,
            account_id=_uuid(_metadata(order.metadata, "account_id")),
            checkout_ref=_uuid(_metadata(order.metadata, "checkout_ref")),
            package_id=package_id,
            amount_paid_minor=order.total_amount,
            currency=order.currency.upper(),
            transaction_id=order.id,
            line_id=order.id,
        )

    @staticmethod
    def _refund(data: dict[str, Any], common: dict[str, Any]) -> ProviderEvent:
        refund = _Refund.model_validate(data)
        if refund.status != _SUCCEEDED:
            return Unknown(**common)
        return RefundApproved(
            **common,
            adjustment_id=refund.id,
            transaction_id=refund.order_id,
            amount_minor=refund.amount + (refund.tax_amount or 0),
            currency=refund.currency.upper(),
        )
