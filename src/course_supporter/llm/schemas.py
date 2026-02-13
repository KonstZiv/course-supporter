"""Shared schemas for LLM infrastructure."""

from datetime import datetime

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    """Input for LLM call."""

    prompt: str
    system_prompt: str | None = None
    model: str = ""  # set by ModelRouter; providers fall back to default_model
    temperature: float = 0.0
    max_tokens: int = 4096
    action: str = ""  # video_analysis, course_structuring, ...
    strategy: str = "default"  # default, quality, budget


class LLMResponse(BaseModel):
    """Unified response from any LLM provider."""

    content: str
    provider: str  # gemini, anthropic, openai, deepseek
    model_id: str  # gemini-2.5-flash, claude-sonnet-4, ...
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=datetime.now)
