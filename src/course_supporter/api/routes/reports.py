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
        summary = await repo.get_summary()
        by_action = await repo.get_by_action()
        by_provider = await repo.get_by_provider()
        by_model = await repo.get_by_model()
    return CostReport(
        summary=summary,
        by_action=by_action,
        by_provider=by_provider,
        by_model=by_model,
    )
