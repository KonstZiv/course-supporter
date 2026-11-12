"""The payment port: what billing asks of a payment provider (BIL-PAY).

Purpose:
    One contract for every merchant of record the platform sells through.
    An adapter speaks one provider's API — its checkout, its webhook
    signature, its refunds — and hands billing the same few values whatever
    the provider; billing never reads a provider's own payload. Today there is
    one adapter, :class:`~course_supporter.billing.payments.fake.FakeProvider`,
    for tests and local development; a real provider is one new adapter
    registered by name (:mod:`.registry`), not an edit here.

The port (:class:`PaymentProvider`, a structural ``Protocol``):

    * ``name`` — the adapter's name: in ``BILLING_PROVIDERS``, in the address
      of its webhook (``/api/v1/billing/webhooks/{name}``), and in the journal
      as the ``provider`` of a batch, a refund, an event.
    * :meth:`~PaymentProvider.create_checkout` — a :class:`CheckoutRequest`
      (the account, the package, the language, where to return) becomes a
      :class:`Checkout` (the link to open, the provider's id of it). The
      account id travels to the provider as custom data and comes back in the
      webhook of the purchase: that is how a payment finds its account.
    * :meth:`~PaymentProvider.parse_webhook` — the request's headers and raw
      body become the normalized events in it. The signature is checked first
      and is never optional: a body without a valid one raises
      :class:`InvalidWebhookError`, and nothing is recorded.
    * :meth:`~PaymentProvider.request_refund` — a :class:`RefundRequest`
      (a purchase batch, the units, the amount the journal computes:
      :func:`refund_request`) becomes the provider's adjustment id.

Normalized events:
    :class:`PurchaseCompleted`, :class:`RefundApproved`,
    :class:`RefundRejected`, :class:`Chargeback`, :class:`Unknown` — the
    union :data:`ProviderEvent`. ``event_id`` is unique per provider and per
    normalized event: when one event of a provider becomes several (a
    transaction of several lines), the adapter gives each its own id (say,
    the event's id and the line's). Amounts are minor units (cents) of an
    ISO 4217 currency; ``amount_paid_minor`` of a purchase is what the payer
    paid, tax included. Ids are 1 to 64 characters; an event that breaks a
    rule of its shape is :class:`InvalidWebhookError`.

Personal data:
    None crosses the port. A provider knows the payer's e-mail, name and
    address; an event carries ids, amounts and times only.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import EventKind
from course_supporter.billing.refunds import refund_amount
from course_supporter.storage.orm import BillingBatch

if TYPE_CHECKING:
    from course_supporter.billing.payments.catalog import Package

MAX_ID_LENGTH: Final = 64
"""The longest id of a provider the journal keeps (``billing_*`` columns)."""

_CURRENCY = re.compile(r"[A-Z]{3}")


# ── Errors ─────────────────────────────────────────────────────────────


class PaymentProviderError(Exception):
    """A provider could not do what was asked (its API failed or refused)."""


class InvalidWebhookError(PaymentProviderError):
    """A webhook's signature is missing or wrong, or its body is not an event
    of the provider: refused, nothing recorded."""


class CheckoutUnavailableError(PaymentProviderError):
    """The provider cannot sell this package (e.g. no price of its own for it)."""


# ── Checkout ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckoutRequest:
    """What a provider needs to open a checkout of one package."""

    account_id: uuid.UUID
    """The account the units go to: sent as custom data, returned in the
    webhook of the purchase."""
    package: Package
    provider_item: str | None
    """The provider's id of the package (its section of the catalog); None
    when the catalog has none."""
    language: str | None
    """The payer's language, ISO 639-3 (``ukr``), when known; the adapter maps
    it to the provider's locale."""
    success_url: str
    cancel_url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkout:
    """A checkout opened at the provider."""

    url: str
    """Where the payer goes to pay."""
    checkout_id: str
    """The provider's id of the checkout (or of the transaction it opens)."""


# ── Refund ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundRequest:
    """What a provider needs to refund units of one purchase batch."""

    batch_id: uuid.UUID
    transaction_id: str
    line_id: str
    units: int
    amount_minor: int
    """The journal's figure: :func:`refund_amount` of the batch and units."""
    currency: str


def refund_request(batch: BillingBatch, units: int) -> RefundRequest:
    """The request of a refund of ``units`` of a purchase batch.

    The amount is the journal's (:func:`refund_amount`: proportional, rounded
    down); the adapter sends it as it is. The order a caller keeps — check
    the free units under the account's lock, ask the provider, then hold
    (:func:`~course_supporter.billing.refunds.refund_hold`) with the
    adjustment id it returns — is the caller's; none exists yet.

    Raises:
        BillingInvalidError: an administrator's credit, units out of range,
            or a purchase without the provider's ids.
    """
    amount = refund_amount(batch, units)
    if (
        batch.provider_transaction_id is None
        or batch.provider_line_id is None
        or batch.currency is None
    ):
        raise BillingInvalidError(
            f"batch {batch.id} has no transaction of its provider to refund"
        )
    return RefundRequest(
        batch_id=batch.id,
        transaction_id=batch.provider_transaction_id,
        line_id=batch.provider_line_id,
        units=units,
        amount_minor=amount,
        currency=batch.currency,
    )


# ── Normalized events ──────────────────────────────────────────────────


def _check_id(value: str | None, what: str) -> None:
    if value is not None and not 0 < len(value) <= MAX_ID_LENGTH:
        raise InvalidWebhookError(f"{what} must be 1 to {MAX_ID_LENGTH} characters")


def _check_money(amount: int | None, currency: str | None) -> None:
    if amount is not None and amount < 0:
        raise InvalidWebhookError("an amount cannot be negative")
    if currency is not None and _CURRENCY.fullmatch(currency) is None:
        raise InvalidWebhookError(f"{currency!r} is not an ISO 4217 code")


def _check_common(event_id: str, provider_type: str, at: datetime | None) -> None:
    _check_id(event_id, "an event id")
    if not provider_type:
        raise InvalidWebhookError("an event has the provider's type")
    if at is not None and at.utcoffset() is None:
        raise InvalidWebhookError("an event's time has a zone")


@dataclass(frozen=True, kw_only=True)
class PurchaseCompleted:
    """A package was paid: its units go to the account."""

    kind: ClassVar[EventKind] = EventKind.PURCHASE_COMPLETED
    event_id: str
    provider_type: str
    """The provider's own name of the event (e.g. ``transaction.completed``)."""
    occurred_at: datetime | None
    account_id: uuid.UUID | None
    """From the custom data of the checkout; None when the event names none
    (the event then fails and waits for a person)."""
    package_id: str
    amount_paid_minor: int
    """What the payer paid, tax included."""
    currency: str
    transaction_id: str
    line_id: str

    def __post_init__(self) -> None:
        _check_common(self.event_id, self.provider_type, self.occurred_at)
        _check_id(self.package_id, "a package id")
        _check_id(self.transaction_id, "a transaction id")
        _check_id(self.line_id, "a line id")
        _check_money(self.amount_paid_minor, self.currency)


@dataclass(frozen=True, kw_only=True)
class RefundApproved:
    """The provider refunded: the held units of the refund leave the batch."""

    kind: ClassVar[EventKind] = EventKind.REFUND_APPROVED
    event_id: str
    provider_type: str
    occurred_at: datetime | None
    adjustment_id: str
    transaction_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.event_id, self.provider_type, self.occurred_at)
        _check_id(self.adjustment_id, "an adjustment id")
        _check_id(self.transaction_id, "a transaction id")
        _check_money(self.amount_minor, self.currency)


@dataclass(frozen=True, kw_only=True)
class RefundRejected:
    """The provider refused the refund: the held units are free again."""

    kind: ClassVar[EventKind] = EventKind.REFUND_REJECTED
    event_id: str
    provider_type: str
    occurred_at: datetime | None
    adjustment_id: str
    transaction_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.event_id, self.provider_type, self.occurred_at)
        _check_id(self.adjustment_id, "an adjustment id")
        _check_id(self.transaction_id, "a transaction id")
        _check_money(self.amount_minor, self.currency)


@dataclass(frozen=True, kw_only=True)
class Chargeback:
    """The payer disputed a payment with their bank. Recorded; the journal is
    not touched — what to do is a person's decision."""

    kind: ClassVar[EventKind] = EventKind.CHARGEBACK
    event_id: str
    provider_type: str
    occurred_at: datetime | None
    account_id: uuid.UUID | None
    transaction_id: str
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        _check_common(self.event_id, self.provider_type, self.occurred_at)
        _check_id(self.transaction_id, "a transaction id")
        _check_money(self.amount_minor, self.currency)


@dataclass(frozen=True, kw_only=True)
class Unknown:
    """Any other event of the provider (``provider_type`` says which): ignored."""

    kind: ClassVar[EventKind] = EventKind.UNKNOWN
    event_id: str
    provider_type: str
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        _check_common(self.event_id, self.provider_type, self.occurred_at)


ProviderEvent = (
    PurchaseCompleted | RefundApproved | RefundRejected | Chargeback | Unknown
)
"""A normalized event, as :meth:`PaymentProvider.parse_webhook` returns it."""


# ── The port ───────────────────────────────────────────────────────────


class PaymentProvider(Protocol):
    """A payment provider's adapter (module docstring)."""

    @property
    def name(self) -> str:
        """The adapter's name: lowercase, as the journal spells a provider."""
        ...

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        """Open a checkout of the package for the account.

        Raises:
            CheckoutUnavailableError: the provider cannot sell this package.
            PaymentProviderError: the provider's API failed.
        """
        ...

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ProviderEvent]:
        """Check the signature, then read the events of the body.

        ``headers`` are read by lowercase name. An event of no interest to
        billing comes back as :class:`Unknown`, not as an error.

        Raises:
            InvalidWebhookError: no signature, a wrong one, or a body that is not
                the provider's event.
        """
        ...

    async def request_refund(self, request: RefundRequest) -> str:
        """Ask the provider to refund; return its adjustment id.

        Raises:
            PaymentProviderError: the provider's API failed or refused.
        """
        ...
