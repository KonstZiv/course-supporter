"""Cost report API endpoints."""

from fastapi import APIRouter

from course_supporter.models.reports import CostReport
from course_supporter.storage.database import async_session
from course_supporter.storage.repositories import LLMCallRepository

router = APIRouter(tags=["reports"])


@router.get("/reports/cost", response_model=CostReport)
async def get_cost_report() -> CostReport:
    """Get LLM call cost report with summary and breakdowns."""
    async with async_session() as session:
        repo = LLMCallRepository(session)
        return await repo.get_full_report()
