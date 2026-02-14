"""Cost report schemas for LLM call analytics."""

from pydantic import BaseModel


class CostSummary(BaseModel):
    """Aggregate summary of all LLM calls."""

    total_calls: int
    successful_calls: int
    failed_calls: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    avg_latency_ms: float


class GroupedCost(BaseModel):
    """Cost breakdown grouped by a single dimension."""

    group: str
    calls: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    avg_latency_ms: float


class CostReport(BaseModel):
    """Full cost report with summary and breakdowns."""

    summary: CostSummary
    by_action: list[GroupedCost]
    by_provider: list[GroupedCost]
    by_model: list[GroupedCost]
