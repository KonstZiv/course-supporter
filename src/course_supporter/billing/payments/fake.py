"""A payment provider that is not one — for tests and local development (BIL-PAY).

:class:`FakeProvider` keeps the port's contract with nothing behind it: a
checkout is a link to nowhere (``.invalid``), a refund gets an adjustment id
at once, and a webhook is signed with a shared test secret — HMAC-SHA256 of
the raw body, hex, in ``X-Fake-Signature``. It remembers its checkouts and
refunds and builds the signed webhooks a provider would send
(:meth:`FakeProvider.purchase_webhook` and the rest), so a test — or a
developer with ``curl`` — walks checkout → payment → units end to end.

Its secret is in this file, so it is never registered outside development
and testing (:mod:`.registry`; a test locks it).

Its webhook body (JSON)::

    {"event_id": "...", "event_type": "purchase.completed",
     "occurred_at": "<ISO 8601>", "data": {...}}

* ``purchase.completed`` — ``data``: ``custom_data`` {``account_id``,
  ``package_id``} (as the checkout sent them), ``transaction_id``,
  ``line_id``, ``amount_minor``, ``currency``;
* ``refund.approved`` / ``refund.rejected`` — ``adjustment_id``, and
  optionally ``transaction_id``, ``amount_minor``, ``currency``;
* ``chargeback.created`` — ``custom_data`` {``account_id``},
  ``transaction_id``, ``amount_minor``, ``currency``, and optionally
  ``dispute_id`` (the event's id when absent);
* any other ``event_type`` — :class:`~.port.Unknown`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel

from course_supporter.billing.payments.catalog import REQUIRED_CURRENCY
from course_supporter.billing.payments.port import (
    Chargeback,
    Checkout,
    CheckoutRequest,
    InvalidWebhookError,
    ProviderEvent,
    PurchaseCompleted,
    RefundApproved,
    RefundRejected,
    RefundRequest,
    Unknown,
)

FAKE: Final = "fake"
"""The adapter's name (``BILLING_PROVIDERS=["fake"]``)."""

FAKE_SECRET: Final = b"course-supporter-fake-provider"
"""The shared test secret. Not a secret: development and testing only."""

SIGNATURE_HEADER: Final = "x-fake-signature"

PURCHASE: Final = "purchase.completed"
REFUND_APPROVED: Final = "refund.approved"
REFUND_REJECTED: Final = "refund.rejected"
CHARGEBACK: Final = "chargeback.created"


class _CustomData(BaseModel):
    account_id: str | None = None
    package_id: str | None = None


class _Purchase(BaseModel):
    custom_data: _CustomData
    transaction_id: str
    line_id: str
    amount_minor: int
    currency: str


class _Refund(BaseModel):
    adjustment_id: str
    transaction_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None


class _Chargeback(BaseModel):
    custom_data: _CustomData = _CustomData()
    dispute_id: str | None = None
    transaction_id: str
    amount_minor: int
    currency: str


class _Envelope(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime | None = None
    data: dict[str, Any] = {}


def _account(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


class FakeProvider:
    """The port's contract with no provider behind it (module docstring)."""

    name: Final = FAKE

    def __init__(self, secret: bytes = FAKE_SECRET) -> None:
        self._secret = secret
        self.checkouts: dict[str, CheckoutRequest] = {}
        """Every checkout opened, by its id."""
        self.refunds: dict[str, RefundRequest] = {}
        """Every refund requested, by its adjustment id."""

    def sign(self, body: bytes) -> str:
        """The signature a webhook of this body carries."""
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    # ── The port ──

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        """A link to nowhere; the request is kept for the purchase's webhook."""
        checkout_id = f"fake_chk_{uuid.uuid4().hex}"
        self.checkouts[checkout_id] = request
        return Checkout(
            url=f"https://checkout.fake.invalid/{checkout_id}",
            checkout_id=checkout_id,
        )

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ProviderEvent]:
        """Check the signature, then read the one event of the body."""
        signature = headers.get(SIGNATURE_HEADER)
        if not signature:
            raise InvalidWebhookError("no signature")
        if not hmac.compare_digest(signature, self.sign(body)):
            raise InvalidWebhookError("a wrong signature")
        try:
            envelope = _Envelope.model_validate(json.loads(body))
            return [self._event(envelope)]
        except ValueError as exc:  # not JSON, or JSON of another shape
            raise InvalidWebhookError("not an event of the fake provider") from exc

    async def request_refund(self, request: RefundRequest) -> str:
        """An adjustment id at once; the request is kept."""
        adjustment_id = f"fake_adj_{uuid.uuid4().hex}"
        self.refunds[adjustment_id] = request
        return adjustment_id

    # ── Playing the provider (tests, local development) ──

    def webhook(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[dict[str, str], bytes]:
        """The headers and body of a signed webhook."""
        body = json.dumps(
            {
                "event_id": event_id or f"fake_evt_{uuid.uuid4().hex}",
                "event_type": event_type,
                "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
                "data": dict(data),
            }
        ).encode()
        return {SIGNATURE_HEADER: self.sign(body)}, body

    def purchase_webhook(
        self,
        checkout_id: str,
        *,
        currency: str = REQUIRED_CURRENCY,
        amount_minor: int | None = None,
        event_id: str | None = None,
    ) -> tuple[dict[str, str], bytes]:
        """The checkout was paid: its account and package come back as sent.

        The amount is the package's price in ``currency`` unless given.
        """
        request = self.checkouts[checkout_id]
        token = checkout_id.removeprefix("fake_chk_")
        amount = (
            request.package.prices_minor[currency]
            if amount_minor is None
            else amount_minor
        )
        return self.webhook(
            PURCHASE,
            {
                "custom_data": {
                    "account_id": str(request.account_id),
                    "package_id": request.package.id,
                },
                "transaction_id": f"fake_txn_{token}",
                "line_id": f"fake_line_{token}",
                "amount_minor": amount,
                "currency": currency,
            },
            event_id=event_id,
        )

    def refund_webhook(
        self, adjustment_id: str, *, approved: bool, event_id: str | None = None
    ) -> tuple[dict[str, str], bytes]:
        """The provider decided a refund."""
        return self.webhook(
            REFUND_APPROVED if approved else REFUND_REJECTED,
            {"adjustment_id": adjustment_id},
            event_id=event_id,
        )

    # ── Reading its body ──

    def _event(self, envelope: _Envelope) -> ProviderEvent:
        common: dict[str, Any] = {
            "event_id": envelope.event_id,
            "provider_type": envelope.event_type,
            "occurred_at": envelope.occurred_at,
        }
        if envelope.event_type == PURCHASE:
            paid = _Purchase.model_validate(envelope.data)
            if not paid.custom_data.package_id:
                raise InvalidWebhookError("a purchase names no package")
            return PurchaseCompleted(
                **common,
                account_id=_account(paid.custom_data.account_id),
                package_id=paid.custom_data.package_id,
                amount_paid_minor=paid.amount_minor,
                currency=paid.currency,
                transaction_id=paid.transaction_id,
                line_id=paid.line_id,
            )
        if envelope.event_type in (REFUND_APPROVED, REFUND_REJECTED):
            refund = _Refund.model_validate(envelope.data)
            decided = (
                RefundApproved
                if envelope.event_type == REFUND_APPROVED
                else RefundRejected
            )
            return decided(
                **common,
                adjustment_id=refund.adjustment_id,
                transaction_id=refund.transaction_id,
                amount_minor=refund.amount_minor,
                currency=refund.currency,
            )
        if envelope.event_type == CHARGEBACK:
            disputed = _Chargeback.model_validate(envelope.data)
            return Chargeback(
                **common,
                dispute_id=disputed.dispute_id or envelope.event_id,
                account_id=_account(disputed.custom_data.account_id),
                transaction_id=disputed.transaction_id,
                amount_minor=disputed.amount_minor,
                currency=disputed.currency,
            )
        return Unknown(**common)
