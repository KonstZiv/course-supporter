"""Billing in the student portal: consent to the terms (BIL-A), packages and
checkout (BIL-PAY).

Routes
------
- ``GET /portal/billing/terms`` — the revision of the billing terms in force
  (``settings.billing_terms_version``) and whether the student still has to
  agree to it. Creates nothing.
- ``POST /portal/billing/consent`` — the student agrees to the revision they
  were shown: 201 with the new record, 200 with the earlier one when they had
  already agreed to it, 409 when the revision is not the one in force (the
  page showed old terms; reload them).
- ``GET /portal/billing/packages`` — the packages on sale: units and prices,
  never a provider's id; and ``purchase`` — whether the student may buy now
  (``open``, else the ``reason``: ``payment_unavailable`` |
  ``purchase_not_open``). Creates no account.
- ``POST /portal/billing/checkout`` — open a checkout of a package for the
  student's account: 503 when payment is unavailable (no provider switched
  on), 409 while the student has not agreed to the terms in force, 403 while
  the provider is in test mode and the account is not marked for test
  purchases, 404 for a package not on sale or a provider not switched on,
  else the link and the checkout's own number (``checkout_ref``), which is
  also the last segment of the address the student comes back to:
  ``{PORTAL_BASE_URL}/{tenant_id}/billing/checkouts/{checkout_ref}``.
- ``GET /portal/billing/checkouts/{checkout_ref}`` — the state of the
  student's checkout (pending | credited | failed); another's or an unknown
  one is 404 ``checkout_not_found``.

Every refusal carries its reason code (``_billing_shared``).

Under the student's own portal session (``get_current_student``), not an
administrator's. The student's billing account is created on the first
consent (``billing.accounts``). The logic is ``billing.consent`` and
``billing.payments.checkout``.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_billing_catalog,
    get_current_student,
    get_payment_registry,
    get_session,
)
from course_supporter.api.routes._billing_shared import (
    CHECKOUT_REFUSALS,
    Refusal,
    checkout_state_of,
    consent_required,
    open_checkout,
    packages_of,
    require_payment,
    responses,
    terms_changed,
)
from course_supporter.api.schemas import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingCheckoutStateResponse,
    BillingConsentRequest,
    BillingConsentResponse,
    BillingPackagesResponse,
    BillingTermsResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.billing.accounts import (
    find_active_account,
    get_or_create_student_account,
)
from course_supporter.billing.consent import (
    record_student_consent,
    student_terms_status,
)
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import AccountKind, ConsentChannel
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.config import get_settings

router = APIRouter(tags=["portal"])
logger = structlog.get_logger()

StudentDep = Annotated[StudentContext, Depends(get_current_student)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_payment_registry)]
CatalogDep = Annotated[Catalog, Depends(get_billing_catalog)]


@router.get("/portal/billing/terms")
async def get_billing_terms(
    student: StudentDep, session: SessionDep
) -> BillingTermsResponse:
    """The billing terms in force and whether the student has agreed to them."""
    status = await student_terms_status(
        session,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
        terms_version=get_settings().billing_terms_version,
    )
    return BillingTermsResponse(
        terms_version=status.terms_version,
        consent_required=status.consent_required,
        consented_at=status.consented_at,
    )


@router.post(
    "/portal/billing/consent",
    status_code=201,
    responses={
        200: {"description": "Already agreed to this revision; the earlier record."},
        **responses(Refusal.TERMS_CHANGED),
    },
)
async def post_billing_consent(
    body: BillingConsentRequest,
    student: StudentDep,
    session: SessionDep,
    response: Response,
) -> BillingConsentResponse:
    """Record the student's consent to the revision in force."""
    current = get_settings().billing_terms_version
    try:
        record, created = await record_student_consent(
            session,
            tenant_id=student.tenant_id,
            student_id=student.student_id,
            terms_version=body.terms_version,
            current_version=current,
            text_sha256=body.text_sha256,
            channel=ConsentChannel.PORTAL,
        )
    except BillingConflictError as exc:
        raise terms_changed(current) from exc
    await session.commit()
    if created:
        logger.info(
            "billing_consent_recorded",
            tenant_id=str(student.tenant_id),
            student_id=str(student.student_id),
            terms_version=record.terms_version,
        )
    else:
        response.status_code = 200
    return BillingConsentResponse(
        terms_version=record.terms_version, consented_at=record.consented_at
    )


@router.get("/portal/billing/packages")
async def get_billing_packages(
    student: StudentDep,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> BillingPackagesResponse:
    """The packages on sale, and whether the student may buy them now."""
    account = await find_active_account(
        session,
        kind=AccountKind.STUDENT,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
    )
    return packages_of(catalog, registry, account)


@router.post("/portal/billing/checkout", responses=responses(*CHECKOUT_REFUSALS))
async def post_billing_checkout(
    body: BillingCheckoutRequest,
    student: StudentDep,
    session: SessionDep,
    registry: RegistryDep,
    catalog: CatalogDep,
) -> BillingCheckoutResponse:
    """Open a checkout of a package; the units go to the student's account."""
    require_payment(registry)
    settings = get_settings()
    current = settings.billing_terms_version
    terms = await student_terms_status(
        session,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
        terms_version=current,
    )
    if terms.consent_required:
        raise consent_required(current)
    account = await get_or_create_student_account(
        session, tenant_id=student.tenant_id, student_id=student.student_id
    )
    await session.commit()  # no transaction stays open across the provider's call
    portal = settings.portal_base_url.rstrip("/")
    return await open_checkout(
        body,
        session=session,
        registry=registry,
        catalog=catalog,
        account=account,
        language=student.preferred_language,
        return_page=f"{portal}/{student.tenant_id}/billing",
    )


@router.get(
    "/portal/billing/checkouts/{checkout_ref}",
    responses=responses(Refusal.CHECKOUT_NOT_FOUND),
)
async def get_billing_checkout(
    checkout_ref: str, student: StudentDep, session: SessionDep
) -> BillingCheckoutStateResponse:
    """The state of the student's checkout by its number."""
    account = await find_active_account(
        session,
        kind=AccountKind.STUDENT,
        tenant_id=student.tenant_id,
        student_id=student.student_id,
    )
    return await checkout_state_of(session, account, checkout_ref)
