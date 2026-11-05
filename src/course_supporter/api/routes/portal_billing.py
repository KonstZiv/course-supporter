"""Billing terms consent in the student portal (BIL-A).

Routes
------
- ``GET /portal/billing/terms`` — the revision of the billing terms in force
  (``settings.billing_terms_version``) and whether the student still has to
  agree to it. Creates nothing.
- ``POST /portal/billing/consent`` — the student agrees to the revision they
  were shown: 201 with the new record, 200 with the earlier one when they had
  already agreed to it, 409 when the revision is not the one in force (the
  page showed old terms; reload them).

Under the student's own portal session (``get_current_student``), not an
administrator's. The student's billing account is created on the first
consent (``billing.accounts``). The logic is ``billing.consent``.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.schemas import (
    BillingConsentRequest,
    BillingConsentResponse,
    BillingTermsResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.billing.consent import (
    record_student_consent,
    student_terms_status,
)
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import ConsentChannel
from course_supporter.config import get_settings

router = APIRouter(tags=["portal"])
logger = structlog.get_logger()

StudentDep = Annotated[StudentContext, Depends(get_current_student)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
