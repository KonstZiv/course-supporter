"""Opening a checkout of a package (BIL-PAY).

Purpose:
    One service for every payer: a student from the portal (the units go to
    the student's account) and an author with the tenant's key (to the
    author's account, which pays for processing and, under a payment rule,
    for the reviews of the tenant's students). The route decides the account
    and whether its holder may pay (a student's consent to the terms in
    force); this module picks the provider and the package and asks the
    provider for a checkout. Nothing is written: the account id travels as
    custom data and comes back with the purchase (:mod:`.webhooks`).

Refusals:
    :class:`PaymentUnavailableError` (503) — no provider is switched on, or
    the one asked cannot sell the package; :class:`CheckoutNotFoundError`
    (404) — no such package on sale, or the provider named is not switched
    on. A provider's API failing raises
    :class:`~.port.PaymentProviderError` (502).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.port import (
    CheckoutRequest,
    CheckoutUnavailableError,
)
from course_supporter.billing.payments.registry import ProviderRegistry

log = structlog.get_logger()


class PaymentUnavailableError(Exception):
    """Payment is unavailable: no provider, or none that sells the package."""


class CheckoutNotFoundError(Exception):
    """No such package on sale, or no such provider switched on."""


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
    account_id: uuid.UUID,
    package_id: str,
    provider_name: str | None,
    language: str | None,
    return_page: str,
) -> CheckoutStarted:
    """Open a checkout of an active package for the account.

    ``provider_name`` None picks the default provider (the first switched
    on); ``language`` is ISO 639-3 or None; ``return_page`` is the billing
    page the payer comes back to (:func:`return_urls`).

    Raises:
        PaymentUnavailableError: no provider is switched on, or the provider
            cannot sell this package.
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
        raise CheckoutNotFoundError(f"provider {provider_name!r} is not switched on")
    package = catalog.package(package_id)
    if package is None or not package.active:
        raise CheckoutNotFoundError(f"package {package_id!r} is not on sale")
    success_url, cancel_url = return_urls(return_page)
    try:
        checkout = await provider.create_checkout(
            CheckoutRequest(
                account_id=account_id,
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
        account_id=str(account_id),
        package_id=package.id,
        checkout_id=checkout.checkout_id,
    )
    return CheckoutStarted(
        provider=provider.name, checkout_id=checkout.checkout_id, url=checkout.url
    )
