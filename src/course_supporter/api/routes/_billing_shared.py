"""What the portal's and the author's checkout routes share (BIL-PAY).

Both open a checkout through the one service
(:func:`course_supporter.billing.payments.checkout.start_checkout`); this
maps its refusals to HTTP the same way for both.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import HTTPException

from course_supporter.api.schemas import BillingCheckoutRequest, BillingCheckoutResponse
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.checkout import (
    CheckoutNotFoundError,
    PaymentUnavailableError,
    start_checkout,
)
from course_supporter.billing.payments.port import PaymentProviderError
from course_supporter.billing.payments.registry import ProviderRegistry

logger = structlog.get_logger()

PAYMENT_UNAVAILABLE = "Payment is unavailable."

CHECKOUT_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "No such package on sale, or no such provider."},
    502: {"description": "The payment provider did not answer."},
    503: {"description": "Payment is unavailable: no provider is switched on."},
}


def require_payment(registry: ProviderRegistry) -> None:
    """503 when no provider is switched on."""
    if not len(registry):
        raise HTTPException(status_code=503, detail=PAYMENT_UNAVAILABLE)


async def open_checkout(
    body: BillingCheckoutRequest,
    *,
    registry: ProviderRegistry,
    catalog: Catalog,
    account_id: uuid.UUID,
    language: str | None,
    return_page: str,
) -> BillingCheckoutResponse:
    """Open the checkout; 404 / 502 / 503 for the service's refusals."""
    try:
        started = await start_checkout(
            registry=registry,
            catalog=catalog,
            account_id=account_id,
            package_id=body.package_id,
            provider_name=body.provider,
            language=language,
            return_page=return_page,
        )
    except PaymentUnavailableError as exc:
        raise HTTPException(status_code=503, detail=PAYMENT_UNAVAILABLE) from exc
    except CheckoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaymentProviderError as exc:
        logger.exception(
            "billing_checkout_provider_failed",
            provider=body.provider or registry.names[0],
            package_id=body.package_id,
        )
        raise HTTPException(
            status_code=502, detail="The payment provider did not answer."
        ) from exc
    return BillingCheckoutResponse(
        provider=started.provider,
        checkout_id=started.checkout_id,
        checkout_url=started.url,
    )
