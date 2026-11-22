"""Opening a checkout of a package (BIL-PAY).

Purpose:
    One service for every payer: a student from the portal (the units go to
    the student's account) and an author with the tenant's key (to the
    author's account, which pays for processing and, under a payment rule,
    for the reviews of the tenant's students). The route decides the account
    and whether its holder may pay (consent to the terms in force); this
    module picks the provider and the package, decides whether the purchase
    is open to the account, asks the provider for a checkout and records it.

The checkout's number (``checkout_ref``):
    The platform's own number of a checkout, whatever the provider: drawn
    before the provider is asked, it travels in the address the payer comes
    back to — in its PATH, ``{billing page}/checkouts/{checkout_ref}``, since a
    provider appends its own query parameters to that address (Creem does,
    and does not say how when the address has a query already) — and in the
    checkout's custom data beside the account and the package. Once the
    provider has opened the checkout, a ``billing_checkouts`` row keeps the
    number with the account, the package, the provider and its id of the
    checkout (:func:`start_checkout`). The purchase's webhook links the batch
    it credited (:func:`link_purchase`); the screen asks the state by the
    number (:func:`checkout_view`): coming back from the provider proves no
    payment.

Test purchases (operator 2026-11-15, 2026-11-18):
    While the provider serving the purchase is in its test mode
    (:attr:`~.port.PaymentProvider.test_mode`), a test card pays and real
    units are credited; in its live mode before it approves the shop
    (``BILLING_PURCHASE_MARKED_ONLY``), its reviewer buys with a 100 %
    discount code. Either way a purchase is a test purchase
    (:func:`is_test_purchase`): open only to an account the administrator
    marked (``billing_accounts.test_purchase_allowed``,
    ``scripts/billing_admin.py test-purchase``) — the provider's reviewer's
    demo author, our own test accounts — and its batch is marked when
    credited (``webhooks.py``). :func:`purchase_closed` is the one place that
    decides who may buy, for both checkouts and for the screens' packages
    block, whatever the provider; in live mode with the setting off the mark
    is not read. The purchase's webhook is not guarded: what was paid is
    credited.

Refusals:
    :class:`PaymentUnavailableError` (503) — no provider is switched on, or
    the one asked cannot sell the package; :class:`PurchaseNotOpenError`
    (403) — a test purchase and the account is not marked;
    :class:`CheckoutNotFoundError` (404) — no such package on sale
    (:class:`PackageNotOnSaleError`), or the provider named is not switched on
    (:class:`ProviderNotSwitchedOnError`). A provider's API failing raises
    :class:`~.port.PaymentProviderError` (502).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import ledger
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.port import (
    MAX_ID_LENGTH,
    CheckoutRequest,
    CheckoutUnavailableError,
    PaymentProvider,
    PaymentProviderError,
    PurchaseCompleted,
)
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.storage.orm import BillingAccount, BillingBatch, BillingCheckout

log = structlog.get_logger()


class PaymentUnavailableError(Exception):
    """Payment is unavailable: no provider, or none that sells the package."""


class PurchaseNotOpenError(Exception):
    """A test purchase, and the account is not marked for test purchases
    (module docstring)."""


class CheckoutNotFoundError(Exception):
    """No such package on sale, or no such provider switched on."""


class PackageNotOnSaleError(CheckoutNotFoundError):
    """No such package, or one no longer on sale."""


class ProviderNotSwitchedOnError(CheckoutNotFoundError):
    """The provider named is not switched on."""


class PurchaseClosed(StrEnum):
    """Why a purchase is closed to an account (the screens' reason codes)."""

    PAYMENT_UNAVAILABLE = "payment_unavailable"
    """No payment provider is switched on."""

    PURCHASE_NOT_OPEN = "purchase_not_open"
    """A test purchase, and the account is not marked."""


def is_test_purchase(provider: PaymentProvider, *, marked_only: bool) -> bool:
    """Whether a purchase through ``provider`` now is a test purchase (module
    docstring): the provider is in its test mode, or ``marked_only``
    (``BILLING_PURCHASE_MARKED_ONLY``,
    :attr:`~.registry.ProviderRegistry.purchase_marked_only`) is on."""
    return provider.test_mode or marked_only


def purchase_closed(
    provider: PaymentProvider | None,
    account: BillingAccount | None,
    *,
    marked_only: bool,
) -> PurchaseClosed | None:
    """Why a purchase through ``provider`` is closed to ``account``; None when
    it is open (module docstring).

    ``provider`` None: none is switched on. ``account`` None: the holder has
    none yet — an account that does not exist is not marked. ``marked_only``:
    as :func:`is_test_purchase`.
    """
    if provider is None:
        return PurchaseClosed.PAYMENT_UNAVAILABLE
    if is_test_purchase(provider, marked_only=marked_only) and not (
        account is not None and account.test_purchase_allowed
    ):
        return PurchaseClosed.PURCHASE_NOT_OPEN
    return None


@dataclass(frozen=True, slots=True)
class CheckoutStarted:
    """The checkout opened at the provider, and recorded."""

    provider: str
    checkout_id: str
    url: str
    checkout_ref: uuid.UUID
    """The platform's own number of the checkout (module docstring)."""


def return_urls(page: str, checkout_ref: uuid.UUID) -> tuple[str, str]:
    """Where the provider sends the payer back — the billing page: (on
    success, with the checkout's number in the path; on cancel)."""
    page = page.rstrip("/")
    return f"{page}/checkouts/{checkout_ref}", f"{page}?checkout=cancelled"


async def start_checkout(
    session: AsyncSession,
    *,
    registry: ProviderRegistry,
    catalog: Catalog,
    account: BillingAccount,
    package_id: str,
    provider_name: str | None,
    language: str | None,
    return_page: str,
    now: datetime | None = None,
) -> CheckoutStarted:
    """Open a checkout of an active package for the account, and record it.

    ``provider_name`` None picks the default provider (the first switched
    on); ``language`` is ISO 639-3 or None; ``return_page`` is the billing
    page the payer comes back to (:func:`return_urls`). ``account`` is read
    as loaded (its id, its mark): nothing is queried, so no transaction is
    open while the provider is asked. The checkout's row is added and flushed
    afterwards; the caller commits.

    Raises:
        PaymentUnavailableError: no provider is switched on, or the provider
            cannot sell this package.
        PurchaseNotOpenError: a test purchase, and the account is not
            marked.
        CheckoutNotFoundError: no such package on sale; the named provider
            is not switched on.
        PaymentProviderError: the provider's API failed.
    """
    if not len(registry):
        raise PaymentUnavailableError("no payment provider is switched on")
    provider = (
        registry.default() if provider_name is None else registry.get(provider_name)
    )
    if provider is None:
        msg = f"provider {provider_name!r} is not switched on"
        raise ProviderNotSwitchedOnError(msg)
    closed = purchase_closed(
        provider, account, marked_only=registry.purchase_marked_only
    )
    if closed is PurchaseClosed.PURCHASE_NOT_OPEN:
        log.info(
            "billing_purchase_not_open",
            provider=provider.name,
            account_id=str(account.id),
        )
        msg = (
            f"{provider.name} is in test mode or purchases are for marked "
            f"accounts only; account {account.id} is not marked"
        )
        raise PurchaseNotOpenError(msg)
    package = catalog.package(package_id)
    if package is None or not package.active:
        raise PackageNotOnSaleError(f"package {package_id!r} is not on sale")
    checkout_ref = uuid.uuid4()
    success_url, cancel_url = return_urls(return_page, checkout_ref)
    try:
        checkout = await provider.create_checkout(
            CheckoutRequest(
                account_id=account.id,
                checkout_ref=checkout_ref,
                package=package,
                provider_item=catalog.provider_item(
                    provider.name, package.id, test_mode=provider.test_mode
                ),
                language=language,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        )
    except CheckoutUnavailableError as exc:
        raise PaymentUnavailableError(str(exc)) from exc
    if not 0 < len(checkout.checkout_id) <= MAX_ID_LENGTH:
        msg = f"{provider.name} answered a checkout id of {len(checkout.checkout_id)}"
        raise PaymentProviderError(msg)
    session.add(
        BillingCheckout(
            id=checkout_ref,
            account_id=account.id,
            package_id=package.id,
            provider=provider.name,
            provider_checkout_id=checkout.checkout_id,
            created_at=now or datetime.now(UTC),
        )
    )
    await session.flush()
    log.info(
        "billing_checkout_opened",
        provider=provider.name,
        account_id=str(account.id),
        package_id=package.id,
        checkout_id=checkout.checkout_id,
        checkout_ref=str(checkout_ref),
    )
    return CheckoutStarted(
        provider=provider.name,
        checkout_id=checkout.checkout_id,
        url=checkout.url,
        checkout_ref=checkout_ref,
    )


# ── The purchase, by the checkout's number ─────────────────────────────


async def link_purchase(
    session: AsyncSession,
    *,
    provider: str,
    event: PurchaseCompleted,
    batch: BillingBatch,
) -> bool:
    """Link the batch a purchase credited to the checkout it answers.

    Called by the purchase's webhook in its transaction, after the credit.
    The checkout must be the one the event names (``checkout_ref``), of the
    batch's account, of this provider and of the event's package; a repeat
    finds the link there is. Anything else — no number (a checkout opened
    before the numbers), an unknown one, one of another account — links
    nothing, with a WARNING: the units are credited all the same, and the
    screen's checkout stays pending.

    Returns:
        True when the checkout is (now, or already) linked to ``batch``.
    """
    ref = event.checkout_ref
    if ref is None:
        return False
    holder = await session.scalar(
        select(BillingCheckout.id).where(BillingCheckout.batch_id == batch.id)
    )
    if holder is None:
        linked = await session.execute(
            update(BillingCheckout)
            .where(
                BillingCheckout.id == ref,
                BillingCheckout.account_id == batch.account_id,
                BillingCheckout.provider == provider,
                BillingCheckout.package_id == event.package_id,
                BillingCheckout.batch_id.is_(None),
            )
            .values(batch_id=batch.id)
            .execution_options(synchronize_session=False)
        )
        if linked.rowcount:  # type: ignore[attr-defined]
            return True
    elif holder == ref:
        return True  # a repeat of the purchase
    log.warning(
        "billing_checkout_not_linked",
        provider=provider,
        event_id=event.event_id,
        checkout_ref=str(ref),
        batch_id=str(batch.id),
        linked_checkout=str(holder) if holder is not None else None,
    )
    return False


class CheckoutState(StrEnum):
    """Where a checkout stands, as the screen asks it."""

    PENDING = "pending"
    """No purchase yet: not paid, not reported yet — or never paid."""

    CREDITED = "credited"
    """The purchase's batch is credited."""

    FAILED = "failed"
    """Only when the provider reports the payment failed. The port has no
    such event yet (Creem sends none), so nothing sets it; no timeout does."""


@dataclass(frozen=True, slots=True)
class CheckoutView:
    """A checkout's state for its payer."""

    state: CheckoutState
    package_id: str
    units: int | None
    """Credited: the batch's units; else None."""
    available: int | None
    """Credited: the account's available units now; else None."""


async def checkout_view(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    checkout_ref: uuid.UUID,
    now: datetime | None = None,
) -> CheckoutView | None:
    """The state of the account's checkout by its number; None when the
    account has no such checkout (another's is no different from none)."""
    checkout = await session.scalar(
        select(BillingCheckout).where(
            BillingCheckout.id == checkout_ref,
            BillingCheckout.account_id == account_id,
        )
    )
    if checkout is None:
        return None
    if checkout.batch_id is None:
        return CheckoutView(
            state=CheckoutState.PENDING,
            package_id=checkout.package_id,
            units=None,
            available=None,
        )
    batch = await session.get(BillingBatch, checkout.batch_id)
    assert batch is not None  # a foreign key; a batch is never deleted alone
    standing = await ledger.balance(session, account_id, now=now)
    return CheckoutView(
        state=CheckoutState.CREDITED,
        package_id=checkout.package_id,
        units=batch.units,
        available=standing.available,
    )
