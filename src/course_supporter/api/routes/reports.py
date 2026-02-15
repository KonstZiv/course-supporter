"""Cost report API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends

from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.models.reports import CostReport
from course_supporter.storage.database import async_session
from course_supporter.storage.repositories import LLMCallRepository

router = APIRouter(tags=["reports"])

TenantDep = Annotated[TenantContext, Depends(get_current_tenant)]


@router.get("/reports/cost", response_model=CostReport)
async def get_cost_report(tenant: TenantDep) -> CostReport:
    """Get LLM call cost report with summary and breakdowns."""
    async with async_session() as session:
        repo = LLMCallRepository(session)
        return await repo.get_full_report()
