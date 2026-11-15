"""What the portal's and the author's billing routes share (BIL-PAY).

Refusals:
    Every refusal of a billing route the screens call (the author's with the
    tenant's key, the student's in the portal) carries a reason code — a
    service key the screen picks its own words by — in the project's form of a
    coded refusal (``api/routes/_author_shared.py``, ``criteria.py``)::

        {"detail": {"code": "<reason>", "details": "<text for the logs>"}}

    :class:`Refusal` is every code; :func:`refusal` builds the answer, and the
    code fixes the status, so one code never stands under two statuses. The
    ``details`` say what happened in English and nothing technical (no
    provider's id, no error of its API). The providers' webhook is not a
    screen's route and keeps its bare statuses.

Packages:
    Both packages routes answer :func:`packages_of`: the catalog's packages
    on sale and whether the holder asking may buy now, through the default
    provider — decided by the very function the checkout obeys
    (``billing.payments.checkout.purchase_closed``).

Checkout:
    Both checkout routes open a checkout through the one service
    (:func:`course_supporter.billing.payments.checkout.start_checkout`);
    :func:`open_checkout` maps its refusals the same way for both, and
    :func:`checkout_state_of` answers both routes of a checkout's state: the
    holder's own checkout, by its number — another's, or an unknown one, is
    ``checkout_not_found`` (404, not 403).

Balance and statement:
    :func:`balance_of` and :func:`statement_of` answer the author's and the
    student's routes from the journal (``billing.statement``): units only. A
    holder without an account yet has nothing: zeros, no lines.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import structlog
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.schemas import (
    BillingBalanceResponse,
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingCheckoutStateResponse,
    BillingNextExpiry,
    BillingPackageResponse,
    BillingPackagesResponse,
    BillingPurchaseState,
    BillingStatementAction,
    BillingStatementItem,
    BillingStatementResponse,
)
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.checkout import (
    PackageNotOnSaleError,
    PaymentUnavailableError,
    ProviderNotSwitchedOnError,
    PurchaseClosed,
    PurchaseNotOpenError,
    checkout_view,
    purchase_closed,
    start_checkout,
)
from course_supporter.billing.payments.port import PaymentProviderError
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.statement import (
    InvalidCursorError,
    holder_balance,
    statement_page,
)
from course_supporter.storage.orm import BillingAccount

logger = structlog.get_logger()


class Refusal(StrEnum):
    """Why a billing route refused — the code a screen reads."""

    PAYMENT_UNAVAILABLE = PurchaseClosed.PAYMENT_UNAVAILABLE.value
    """No payment provider is switched on, or none sells the package (503)."""

    PURCHASE_NOT_OPEN = PurchaseClosed.PURCHASE_NOT_OPEN.value
    """The provider is in test mode and the account is not marked for test
    purchases (403): the screen says the purchase is not open yet."""

    TERMS_CONSENT_REQUIRED = "terms_consent_required"
    """A purchase before the holder agreed to the terms in force (409)."""

    TERMS_CHANGED = "terms_changed"
    """A consent to a revision that is not the one in force (409)."""

    PACKAGE_NOT_FOUND = "package_not_found"
    """No such package on sale (404)."""

    PROVIDER_NOT_FOUND = "provider_not_found"
    """The payment provider named is not switched on (404)."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The payment provider did not answer (502)."""

    CHECKOUT_NOT_FOUND = "checkout_not_found"
    """No checkout of the holder's by that number (404): another's is none."""

    INVALID_CURSOR = "invalid_cursor"
    """A statement cursor this server did not give (422)."""


STATUS: Final[dict[Refusal, int]] = {
    Refusal.PAYMENT_UNAVAILABLE: 503,
    Refusal.PURCHASE_NOT_OPEN: 403,
    Refusal.TERMS_CONSENT_REQUIRED: 409,
    Refusal.TERMS_CHANGED: 409,
    Refusal.PACKAGE_NOT_FOUND: 404,
    Refusal.PROVIDER_NOT_FOUND: 404,
    Refusal.PROVIDER_UNAVAILABLE: 502,
    Refusal.CHECKOUT_NOT_FOUND: 404,
    Refusal.INVALID_CURSOR: 422,
}
"""The status of each code (module docstring)."""

PAYMENT_UNAVAILABLE = "Payment is unavailable."
PURCHASE_NOT_OPEN = "Purchase is not open yet."
PROVIDER_DID_NOT_ANSWER = "The payment provider did not answer."
NO_SUCH_CHECKOUT = "No such checkout."
NOT_A_CURSOR = "Not a cursor of this statement: start again without one."


def refusal(code: Refusal, details: str) -> HTTPException:
    """The refusal of ``code`` (module docstring): raise it."""
    return HTTPException(
        status_code=STATUS[code], detail={"code": code.value, "details": details}
    )


def responses(*codes: Refusal) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI ``responses`` of a route that refuses with ``codes``."""
    found: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        status = STATUS[code]
        listed = found.setdefault(status, {"description": "Refused; detail.code:"})
        listed["description"] += f" {code.value}"
    return found


CHECKOUT_REFUSALS: Final = (
    Refusal.PAYMENT_UNAVAILABLE,
    Refusal.PURCHASE_NOT_OPEN,
    Refusal.TERMS_CONSENT_REQUIRED,
    Refusal.PACKAGE_NOT_FOUND,
    Refusal.PROVIDER_NOT_FOUND,
    Refusal.PROVIDER_UNAVAILABLE,
)
"""What a checkout route refuses with."""


def packages_of(
    catalog: Catalog, registry: ProviderRegistry, account: BillingAccount | None
) -> BillingPackagesResponse:
    """The packages on sale (units and prices, never a provider's id) and
    whether ``account`` — the holder's, None while they have none — may buy
    through the default provider."""
    closed = purchase_closed(registry.default(), account)
    return BillingPackagesResponse(
        packages=[
            BillingPackageResponse(
                id=package.id,
                units=package.units,
                prices_minor=dict(package.prices_minor),
            )
            for package in catalog.active()
        ],
        purchase=BillingPurchaseState(
            open=closed is None, reason=None if closed is None else closed.value
        ),
    )


def require_payment(registry: ProviderRegistry) -> None:
    """``payment_unavailable`` when no provider is switched on."""
    if not len(registry):
        raise refusal(Refusal.PAYMENT_UNAVAILABLE, PAYMENT_UNAVAILABLE)


def consent_required(current: str) -> HTTPException:
    """``terms_consent_required``: a purchase before the consent."""
    return refusal(
        Refusal.TERMS_CONSENT_REQUIRED,
        f"Agree to the billing terms first: the current revision is {current}.",
    )


def terms_changed(current: str) -> HTTPException:
    """``terms_changed``: a consent to a revision not in force."""
    return refusal(
        Refusal.TERMS_CHANGED,
        f"The billing terms changed: the current revision is {current}.",
    )


async def open_checkout(
    body: BillingCheckoutRequest,
    *,
    session: AsyncSession,
    registry: ProviderRegistry,
    catalog: Catalog,
    account: BillingAccount,
    language: str | None,
    return_page: str,
) -> BillingCheckoutResponse:
    """Open the checkout and commit its record; the service's refusals as
    their codes."""
    try:
        started = await start_checkout(
            session,
            registry=registry,
            catalog=catalog,
            account=account,
            package_id=body.package_id,
            provider_name=body.provider,
            language=language,
            return_page=return_page,
        )
    except PaymentUnavailableError as exc:
        raise refusal(Refusal.PAYMENT_UNAVAILABLE, PAYMENT_UNAVAILABLE) from exc
    except PurchaseNotOpenError as exc:
        raise refusal(Refusal.PURCHASE_NOT_OPEN, PURCHASE_NOT_OPEN) from exc
    except PackageNotOnSaleError as exc:
        raise refusal(Refusal.PACKAGE_NOT_FOUND, str(exc)) from exc
    except ProviderNotSwitchedOnError as exc:
        raise refusal(Refusal.PROVIDER_NOT_FOUND, str(exc)) from exc
    except PaymentProviderError as exc:
        logger.exception(
            "billing_checkout_provider_failed",
            provider=body.provider or registry.names[0],
            package_id=body.package_id,
        )
        raise refusal(Refusal.PROVIDER_UNAVAILABLE, PROVIDER_DID_NOT_ANSWER) from exc
    await session.commit()
    return BillingCheckoutResponse(
        provider=started.provider,
        checkout_id=started.checkout_id,
        checkout_url=started.url,
        checkout_ref=started.checkout_ref,
    )


async def checkout_state_of(
    session: AsyncSession, account: BillingAccount | None, checkout_ref: str
) -> BillingCheckoutStateResponse:
    """The state of the holder's checkout ``checkout_ref``; ``account`` is
    the holder's, None while they have none.

    Raises:
        HTTPException: ``checkout_not_found`` — not a number of the holder's
            checkouts (another's, an unknown one, or not a number at all).
    """
    try:
        ref = uuid.UUID(checkout_ref)
    except ValueError:
        ref = None
    view = (
        None
        if ref is None or account is None
        else await checkout_view(session, account_id=account.id, checkout_ref=ref)
    )
    if view is None:
        raise refusal(Refusal.CHECKOUT_NOT_FOUND, NO_SUCH_CHECKOUT)
    return BillingCheckoutStateResponse(
        state=view.state.value,
        package_id=view.package_id,
        units=view.units,
        balance_after=view.available,
    )


async def balance_of(
    session: AsyncSession, account: BillingAccount | None
) -> BillingBalanceResponse:
    """The holder's balance; zeros while they have no account."""
    if account is None:
        return BillingBalanceResponse(
            balance=0, reserved=0, held=0, available=0, difference=0, next_expiry=None
        )
    found = await holder_balance(session, account, now=datetime.now(UTC))
    return BillingBalanceResponse(
        balance=found.balance,
        reserved=found.reserved,
        held=found.held,
        available=found.available,
        difference=found.difference,
        next_expiry=(
            None
            if found.next_expiry is None
            else BillingNextExpiry(
                units=found.next_expiry.units,
                expires_on=found.next_expiry.expires_on,
            )
        ),
    )


def _utc(value: datetime | None) -> datetime | None:
    """A bound of the statement; without a zone it is UTC."""
    if value is None or value.utcoffset() is not None:
        return value
    return value.replace(tzinfo=UTC)


async def statement_of(
    session: AsyncSession,
    account: BillingAccount | None,
    *,
    limit: int,
    cursor: str | None,
    since: datetime | None,
    until: datetime | None,
) -> BillingStatementResponse:
    """One page of the holder's statement; empty while they have no account.

    Raises:
        HTTPException: ``invalid_cursor`` — a cursor this server did not give.
    """
    if account is None:
        return BillingStatementResponse(items=[], next_cursor=None)
    try:
        page = await statement_page(
            session,
            account.id,
            limit=limit,
            cursor=cursor,
            since=_utc(since),
            until=_utc(until),
        )
    except InvalidCursorError as exc:
        raise refusal(Refusal.INVALID_CURSOR, NOT_A_CURSOR) from exc
    return BillingStatementResponse(
        items=[
            BillingStatementItem(
                at=line.at,
                kind=line.kind,
                affects_balance=line.affects_balance,
                units=line.units,
                balance_after=line.balance_after,
                action=(
                    None
                    if line.action_kind is None or line.action_id is None
                    else BillingStatementAction(
                        kind=line.action_kind, id=line.action_id
                    )
                ),
            )
            for line in page.lines
        ],
        next_cursor=page.next_cursor,
    )
