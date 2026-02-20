"""Job status API endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import JobResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.job_repository import JobRepository

router = APIRouter(tags=["jobs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SharedDep = Annotated[TenantContext, Depends(require_scope("prep", "check"))]


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> JobResponse:
    """Get job status by ID.

    Tenant isolation enforced via job.course_id → course.tenant_id.
    Returns 404 if the job does not exist or does not belong to the
    current tenant.
    """
    repo = JobRepository(session)
    job = await repo.get_by_id_for_tenant(job_id, tenant.tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.model_validate(job)
