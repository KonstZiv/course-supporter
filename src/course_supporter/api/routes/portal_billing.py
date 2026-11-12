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
  never a provider's id.
- ``POST /portal/billing/checkout`` — open a checkout of a package for the
  student's account: 503 when payment is unavailable (no provider switched
  on), 409 while the student has not agreed to the terms in force, 404 for a
  package not on sale or a provider not switched on, else the link.

Under the student's own portal session (``get_current_student``), not an
administrator's. The student's billing account is created on the first
consent (``billing.accounts``). The logic is ``billing.consent`` and
``billing.payments.checkout``.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_billing_catalog,
    get_current_student,
    get_payment_registry,
    get_session,
)
from course_supporter.api.routes._billing_shared import (
    CHECKOUT_RESPONSES,
    open_checkout,
    require_payment,
)
from course_supporter.api.schemas import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingConsentRequest,
    BillingConsentResponse,
    BillingPackageResponse,
    BillingPackagesResponse,
    BillingTermsResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.billing.accounts import get_or_create_student_account
from course_supporter.billing.consent import (
    record_student_consent,
    student_terms_status,
)
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import ConsentChannel
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
        409: {"description": "Not the revision in force."},
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
        raise HTTPException(
            status_code=409,
            detail=f"The billing terms changed: the current revision is {current}.",
        ) from exc
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


@router.get("/portal/billing/packages", dependencies=[Depends(get_current_student)])
async def get_billing_packages(catalog: CatalogDep) -> BillingPackagesResponse:
    """The packages on sale: units and prices, never a provider's id."""
    return BillingPackagesResponse(
        packages=[
            BillingPackageResponse(
                id=package.id,
                units=package.units,
                prices_minor=dict(package.prices_minor),
            )
            for package in catalog.active()
        ]
    )


@router.post(
    "/portal/billing/checkout",
    responses={
        **CHECKOUT_RESPONSES,
        409: {"description": "The student has not agreed to the terms in force."},
    },
)
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
        raise HTTPException(
            status_code=409,
            detail=f"Agree to the billing terms first: the current revision is "
            f"{current}.",
        )
    account = await get_or_create_student_account(
        session, tenant_id=student.tenant_id, student_id=student.student_id
    )
    response = await open_checkout(
        body,
        registry=registry,
        catalog=catalog,
        account_id=account.id,
        language=student.preferred_language,
        return_page=f"{settings.portal_base_url.rstrip('/')}/{student.tenant_id}"
        "/billing",
    )
    await session.commit()
    return response
