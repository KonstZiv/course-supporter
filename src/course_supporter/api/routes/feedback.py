"""The caller's channel door of a touch (mentor-rebuild task 05).

The mirror of the portal door, and of the submission path before it: mode-1
authenticates with a tenant API key and names the student by the identifier the
caller's own system uses. Everything after that is the shared core, so the two
doors cannot answer the same situation differently.

One difference from the submission path is deliberate: a submission CREATES the
student it names, a touch does not. A touch is an answer about work that was
already done, so a student nobody has heard of has nothing to answer about —
and saying so in any way other than the one generic "not found" would turn this
route into a way of asking which external identifiers exist.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.routes._feedback_shared import to_touch
from course_supporter.api.schemas import ChannelTouchRequest, FeedbackTouch
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.feedback_core import (
    SUBMISSION_NOT_FOUND,
    touch_review,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.student_repository import StudentRepository

logger = structlog.get_logger()

router = APIRouter(tags=["feedback"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CheckDep = Annotated[TenantContext, Depends(require_scope(AuthScope.CHECK))]


@router.post(
    "/feedback/submissions/{submission_id}",
    response_model=FeedbackTouch,
)
async def touch_submission_review(
    tenant: CheckDep,
    session: SessionDep,
    body: ChannelTouchRequest,
    submission_id: Annotated[
        uuid.UUID,
        Path(description="The submission whose review is being answered."),
    ],
) -> FeedbackTouch:
    """Answer whether this review helped — from the caller's own channel.

    The student is resolved by ``student_external_id`` WITHIN the key's tenant
    and is never created: the pair (tenant, external id) is unique, so the
    lookup either names exactly one student of this tenant or names nobody.

    Everything that does not resolve — an unknown external id, a submission of
    another student, an unknown submission id, a soft-deleted one, or a key of
    another tenant, which cannot resolve the student in the first place — ends
    as the SAME generic 404 the portal gives, with the same body. The scope is
    CHECK, the scope the caller's submissions already travel on.
    """
    student = await StudentRepository(session).get_by_external_id(
        tenant.tenant_id, body.student_external_id
    )
    if student is None:
        # Nothing to name into the core, so the refusal is given here — with
        # the core's OWN constant, not a copy of its wording, so the two doors
        # cannot drift into two different sentences for the same situation.
        raise HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)

    submission = await HomeworkRepository(session).get_owned(submission_id, student.id)
    # Defence in depth, and the reason it can only ever be that: a student
    # belongs to exactly one tenant, so a submission owned by a student of this
    # tenant is of this tenant. The check costs nothing and holds if that ever
    # stops being true.
    if submission is not None and submission.tenant_id != tenant.tenant_id:
        submission = None

    row = await touch_review(
        session,
        submission=submission,
        student_id=student.id,
        value=body.value,
    )
    await session.commit()
    return to_touch(row)
