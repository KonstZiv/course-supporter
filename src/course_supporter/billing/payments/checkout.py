"""Opening a checkout of a package (BIL-PAY).

Purpose:
    One service for every payer: a student from the portal (the units go to
    the student's account) and an author with the tenant's key (to the
    author's account, which pays for processing and, under a payment rule,
    for the reviews of the tenant's students). The route decides the account
    and whether its holder may pay (consent to the terms in force); this
    module picks the provider and the package, decides whether the purchase
    is open to the account, and asks the provider for a checkout. Nothing is
    written: the account id travels as custom data and comes back with the
    purchase (:mod:`.webhooks`).

Test mode (operator 2026-11-15):
    While the provider serving the purchase is in its test mode
    (:attr:`~.port.PaymentProvider.test_mode` — on production, for the
    provider's review of the shop), a test card pays and real units are
    credited. So the purchase is open then only to an account the
    administrator marked (``billing_accounts.test_purchase_allowed``,
    ``scripts/billing_admin.py test-purchase``): the provider's reviewer's
    demo author, our own test accounts. :func:`purchase_closed` is the one
    place that decides it, for both checkouts and for the screens' packages
    block, whatever the provider; in live mode the mark is not read. The
    purchase's webhook is not guarded: what was paid is credited.

Refusals:
    :class:`PaymentUnavailableError` (503) — no provider is switched on, or
    the one asked cannot sell the package; :class:`PurchaseNotOpenError`
    (403) — the provider is in test mode and the account is not marked;
    :class:`CheckoutNotFoundError` (404) — no such package on sale
    (:class:`PackageNotOnSaleError`), or the provider named is not switched on
    (:class:`ProviderNotSwitchedOnError`). A provider's API failing raises
    :class:`~.port.PaymentProviderError` (502).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.port import (
    CheckoutRequest,
    CheckoutUnavailableError,
    PaymentProvider,
)
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.storage.orm import BillingAccount

log = structlog.get_logger()


class PaymentUnavailableError(Exception):
    """Payment is unavailable: no provider, or none that sells the package."""


class PurchaseNotOpenError(Exception):
    """The provider is in test mode and the account is not marked for test
    purchases (module docstring)."""


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
    """The provider is in test mode and the account is not marked."""


def purchase_closed(
    provider: PaymentProvider | None, account: BillingAccount | None
) -> PurchaseClosed | None:
    """Why a purchase through ``provider`` is closed to ``account``; None when
    it is open (module docstring).

    ``provider`` None: none is switched on. ``account`` None: the holder has
    none yet — an account that does not exist is not marked.
    """
    if provider is None:
        return PurchaseClosed.PAYMENT_UNAVAILABLE
    if provider.test_mode and not (
        account is not None and account.test_purchase_allowed
    ):
        return PurchaseClosed.PURCHASE_NOT_OPEN
    return None


@dataclass(frozen=True, slots=True)
class CheckoutStarted:
    """The checkout opened at the provider."""

    provider: str
    checkout_id: str
    url: str


def return_urls(page: str) -> tuple[str, str]:
    """Where the provider sends the payer back — the billing page, told how
    it ended: (on success, on cancel)."""
    page = page.rstrip("/")
    return f"{page}?checkout=success", f"{page}?checkout=cancelled"


async def start_checkout(
    *,
    registry: ProviderRegistry,
    catalog: Catalog,
    account: BillingAccount,
    package_id: str,
    provider_name: str | None,
    language: str | None,
    return_page: str,
) -> CheckoutStarted:
    """Open a checkout of an active package for the account.

    ``provider_name`` None picks the default provider (the first switched
    on); ``language`` is ISO 639-3 or None; ``return_page`` is the billing
    page the payer comes back to (:func:`return_urls`). ``account`` is read
    as loaded (its id, its mark); nothing is queried.

    Raises:
        PaymentUnavailableError: no provider is switched on, or the provider
            cannot sell this package.
        PurchaseNotOpenError: the provider is in test mode and the account
            is not marked.
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
    if purchase_closed(provider, account) is PurchaseClosed.PURCHASE_NOT_OPEN:
        log.info(
            "billing_purchase_not_open",
            provider=provider.name,
            account_id=str(account.id),
        )
        msg = f"{provider.name} is in test mode; account {account.id} is not marked"
        raise PurchaseNotOpenError(msg)
    package = catalog.package(package_id)
    if package is None or not package.active:
        raise PackageNotOnSaleError(f"package {package_id!r} is not on sale")
    success_url, cancel_url = return_urls(return_page)
    try:
        checkout = await provider.create_checkout(
            CheckoutRequest(
                account_id=account.id,
                package=package,
                provider_item=catalog.provider_item(provider.name, package.id),
                language=language,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        )
    except CheckoutUnavailableError as exc:
        raise PaymentUnavailableError(str(exc)) from exc
    log.info(
        "billing_checkout_opened",
        provider=provider.name,
        account_id=str(account.id),
        package_id=package.id,
        checkout_id=checkout.checkout_id,
    )
    return CheckoutStarted(
        provider=provider.name, checkout_id=checkout.checkout_id, url=checkout.url
    )
